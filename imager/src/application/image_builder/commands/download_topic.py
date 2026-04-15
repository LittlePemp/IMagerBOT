from src.infrastructure.data.image_builder.unit_of_work import get_uow
from src.infrastructure.services.topic_downloader import FonwallTopicDownloader
from src.shared_kernel.result import Result

from ..errors.commands_errors import CommandsErrorMessages
from ..interfaces_cqrs import ICommand, ICommandHandler


class DownloadTopicCommand(ICommand):
    def __init__(
        self,
        keyword: str,
        group_name: str | None = None,
        limit: int | None = None,
        max_pages: int = 25,
        sync_to_db: bool = True,
    ):
        self.keyword = keyword
        self.group_name = group_name
        self.limit = limit
        self.max_pages = max_pages
        self.sync_to_db = sync_to_db


class DownloadTopicCommandHandler(ICommandHandler):
    def __init__(self, downloader: FonwallTopicDownloader | None = None):
        self.downloader = downloader or FonwallTopicDownloader()

    def handle(self, command: DownloadTopicCommand) -> Result:
        try:
            download_result = self.downloader.download_topic(
                keyword=command.keyword,
                group_name=command.group_name,
                limit=command.limit,
                max_pages=command.max_pages,
            )
            if not download_result.is_success:
                return download_result

            stats = download_result.value
            if command.sync_to_db:
                with get_uow() as uow:
                    sync_result = uow.cell_repository.load_missing_groups(
                        group_names=[stats.group_name]
                    )
                if not sync_result.is_success:
                    return sync_result

            return Result.Success(stats.to_dict())
        except Exception as e:
            return CommandsErrorMessages.general_error(e)
