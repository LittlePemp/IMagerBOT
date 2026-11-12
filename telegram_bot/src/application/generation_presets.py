"""Named profiles assembled from the existing editable parameter records."""
import asyncio
import tomllib
from pathlib import Path

from src.application.generation import validate_output
from src.models.image_build_params import CommonParam, ParamType, validate_display_name


PROFILE_TYPES = (ParamType.IMAGE_SIZE, ParamType.NOISE_LEVEL, ParamType.INSET_SIZE)
DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / 'config' / 'generation_presets.toml'


def load_profile_defaults(path=DEFAULT_PROFILE_PATH):
    """Validate every seed value before the first database write."""
    with Path(path).open('rb') as stream:
        document = tomllib.load(stream)
    recommended = validate_display_name(document['recommended'])
    profiles = []
    names = set()
    for row in document['profiles']:
        name = validate_display_name(row['name'])
        if name in names or set(row) != {'name', *(kind.value for kind in PROFILE_TYPES)}:
            raise ValueError('Profile names must be unique and contain all three parameters')
        parameters = {
            kind.value: CommonParam(name=name, type=kind, value=row[kind.value]).model_dump(mode='json')
            for kind in PROFILE_TYPES
        }
        validate_output(parameters[ParamType.IMAGE_SIZE.value]['value'],
                        parameters[ParamType.INSET_SIZE.value]['value'])
        profiles.append({'name': name, 'parameters': parameters})
        names.add(name)
    if recommended not in names:
        raise ValueError('The recommended profile must exist')
    return recommended, profiles


async def list_generation_profiles(repository):
    results = await asyncio.gather(*(repository.get_common_params(kind) for kind in PROFILE_TYPES))
    if any(not result.is_success for result in results):
        return []
    by_type = {
        kind: {item.name: item for item in result.value}
        for kind, result in zip(PROFILE_TYPES, results)
    }
    names = set.intersection(*(set(values) for values in by_type.values()))
    profiles = [
        {'name': name, 'parameters': {
            kind.value: by_type[kind][name].model_dump(mode='json') for kind in PROFILE_TYPES
        }}
        for name in names
    ]
    return sorted(profiles, key=lambda profile: (
        -profile['parameters'][ParamType.IMAGE_SIZE.value]['value'], profile['name'],
    ))


async def seed_generation_presets(collection, path=DEFAULT_PROFILE_PATH):
    _, profiles = load_profile_defaults(path)
    for profile in profiles:
        for parameter in profile['parameters'].values():
            await collection.update_one(
                {'type': parameter['type'], 'name': parameter['name']},
                {'$set': parameter}, upsert=True,
            )
    return len(profiles)
