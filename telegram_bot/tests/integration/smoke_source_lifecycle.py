"""Opt-in source cleanup smoke on disposable localhost Mongo and MinIO.

Creates and removes its own random database, bucket and temporary files.
    python smoke_source_lifecycle.py mongodb://127.0.0.1:27199 127.0.0.1:9199 ACCESS SECRET
"""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from pathlib import Path
import struct
import sys
import tempfile
import threading
import types
from urllib.parse import urlsplit
from uuid import uuid4
import zlib

REPOSITORY=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(REPOSITORY/'telegram_bot'),str(REPOSITORY)]
# This smoke has no Telegram client or bot token. Production transfer and
# persistence adapters run unchanged; only file logging is replaced.
loggers=types.ModuleType('src.utils.loggers')
for name in ('service_logger','exception_logger','db_logger','bot_requests_logger'):
    setattr(loggers,name,logging.getLogger('source-lifecycle-smoke'))
sys.modules['src.utils.loggers']=loggers

from pymongo import AsyncMongoClient
from imager_config.loader import config
from imager_contracts import make_submission
from src.application.source_lifecycle import SourceLifecycle
from src.infrastructure.data.repositories.admission_repository import AdmissionRepository
from src.infrastructure.services.object_storage import BotObjectStorage
from src.infrastructure.services.storage_janitor import StorageJanitor


def png():
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',20,20,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress((b'\0'+b'\x10\x20\x30'*20)*20))+chunk(b'IEND',b''))


async def main(uri,endpoint,access,secret):
    if urlsplit(uri).hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('Only a disposable localhost Mongo endpoint is supported')
    owner='source_smoke_'+uuid4().hex
    client=AsyncMongoClient(uri,tz_aware=True,serverSelectionTimeoutMS=5000)
    database=client[owner]
    store=BotObjectStorage(endpoint,access,secret,'source-'+uuid4().hex,secure=False)
    admissions=AdmissionRepository(database['admissions'],timezone='Europe/Moscow',daily_limit=5,
                                    lease_seconds=60,retry_seconds=3)
    release_delete=threading.Event()
    task=None
    try:
        await admissions.initialize_indexes()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.png';path.write_bytes(png());job_id=str(uuid4())
            old=await asyncio.to_thread(store.upload_source,path,job_id,content_type='image/png')
            deleting=threading.Event()

            def old_process_delete():
                deleting.set()
                if not release_delete.wait(10):raise TimeoutError('Delayed DELETE was not released')
                # Model a request from a stopped process reaching S3 late.
                return store.delete_source(job_id,old['key'])

            task=asyncio.create_task(asyncio.to_thread(old_process_delete))
            assert await asyncio.to_thread(deleting.wait,2)
            fresh=await asyncio.to_thread(store.upload_source,path,job_id,content_type='image/png')
            assert fresh['key']!=old['key'] and fresh['sha256']==old['sha256']
            request=make_submission({
                'job_id':job_id,'source':fresh,
                'parameters':{'group_name':'fixture','insertion_format':'crop','alpha_channel':30,
                              'noise_level':0,'cell_size':32,'result_size':32},
                'catalog_manifest':{'bucket':store.bucket,'key':'catalog/manifests/fixture.json',
                    'size_bytes':2,'sha256':hashlib.sha256(b'{}').hexdigest(),'content_type':'application/json'},
                'seed':123,'client_id':'telegram','client_context':{'user_id':1,'chat_id':1},
            })
            await admissions.accept_submission(request,actor_id=1)
            release_delete.set();assert await asyncio.wait_for(task,5)
            assert await admissions.source_is_referenced(store.bucket,fresh['key'])
            assert not await admissions.source_is_referenced(store.bucket,old['key'])
            assert store.client.stat_object(store.bucket,fresh['key']).size==len(png())

            orphan=await asyncio.to_thread(store.upload_source,path,job_id,content_type='image/png')

            class Gateway:
                source_bucket=store.bucket

                async def list_sources(self):
                    return await asyncio.to_thread(lambda:list(store.list_sources()))

                async def source_is_referenced(self,key):
                    return await database['jobs'].find_one({'request.source.key':key}) is not None

                async def delete_source(self,job_id,key,*,older_than=None):
                    return await asyncio.to_thread(store.delete_source,job_id,key,older_than=older_than)

            later=datetime.now(timezone.utc)+timedelta(seconds=config.bot.orphan_source_grace_seconds+60)
            janitor=StorageJanitor(Gateway(),admissions,local_api=False,source_lifecycle=SourceLifecycle(),clock=lambda:later)
            assert await janitor.sweep_orphan_sources()==1
            keys={key for key,_ in store.list_sources()}
            assert fresh['key'] in keys and orphan['key'] not in keys
            assert await janitor.sweep_orphan_sources()==0
            print('source lifecycle smoke: OK (late DELETE fenced; accepted source pinned; old attempt removed)')
    finally:
        release_delete.set()
        if task is not None:
            await asyncio.gather(task,return_exceptions=True)
        if store.client.bucket_exists(store.bucket):
            for row in store.client.list_objects(store.bucket,recursive=True):
                store.client.remove_object(store.bucket,row.object_name)
            store.client.remove_bucket(store.bucket)
        await client.drop_database(owner);await client.close()


if __name__=='__main__':asyncio.run(main(*sys.argv[1:5]))
