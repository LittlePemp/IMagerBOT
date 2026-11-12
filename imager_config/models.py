"""Immutable application configuration. No environment or filesystem access."""
from dataclasses import dataclass


WORKER_MEMORY_CEILING_BYTES = 1_000_000_000


@dataclass(frozen=True)
class RuntimeLimitsConfig:
    memory_bytes: int
    memory_headroom_bytes: int
    original_cache_bytes: int
    prepared_cache_bytes: int
    source_file_bytes: int
    source_pixels: int
    output_pixels: int
    cells: int
    decoder_width: int
    decoder_height: int
    cell_size: int
    result_size: int


@dataclass(frozen=True)
class RuntimeMemoryConfig:
    decode_reserve_bytes: int
    manifest_reserve_bytes: int
    catalog_check_reserve_bytes: int
    encode_reserve_bytes: int
    cache_eviction_step_bytes: int
    render_bytes_per_pixel: int


@dataclass(frozen=True)
class RuntimeRenderConfig:
    jpeg_quality: int
    max_output_bytes: int
    progress_interval_cells: int
    output_extension: str
    estimated_jpeg_bytes_per_pixel: float
    output_stem: str
    output_stem_max_length: int
    output_random_suffix_length: int
    output_timestamp_format: str
    output_filename_template: str


@dataclass(frozen=True)
class RuntimeImageConfig:
    min_width: int
    min_height: int
    max_width: int
    max_height: int
    aspect_ratio_min: float
    aspect_ratio_max: float
    allowed_color_modes: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeStorageConfig:
    endpoint: str
    bucket: str
    secure: bool
    connect_timeout_seconds: float
    read_timeout_seconds: float
    retry_attempts: int
    retry_backoff_seconds: float
    transfer_chunk_bytes: int
    upload_parallelism: int
    source_filename: str
    upload_filename: str
    cell_filename: str
    manifest_filename: str
    result_filename: str
    source_key_template: str
    result_key_template: str
    catalog_image_key_template: str
    catalog_manifest_key_template: str
    render_temp_prefix: str
    cell_temp_prefix: str
    manifest_temp_prefix: str
    json_temp_prefix: str
    download_temp_prefix: str
    upload_temp_prefix: str
    result_temp_prefix: str
    local_result_temp_prefix: str
    rpc_temp_prefix: str


@dataclass(frozen=True)
class RuntimeCatalogConfig:
    max_manifest_bytes: int
    max_images: int
    validation_interval: int
    allowed_extensions: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeMongoConfig:
    connect_timeout_ms: int
    socket_timeout_ms: int
    server_selection_timeout_ms: int
    wait_queue_timeout_ms: int
    core_database: str
    bot_database: str
    jobs_collection: str
    catalog_collection: str


@dataclass(frozen=True)
class RuntimeJobsConfig:
    execution_lease_seconds: int
    dispatch_retry_seconds: int
    error_max_length: int
    heartbeat_interval_seconds: float
    heartbeat_renew_divisor: float
    heartbeat_join_seconds: float
    claim_wait_seconds: float


@dataclass(frozen=True)
class RuntimeTemporalWorkerConfig:
    reconnect_seconds: float
    identity_fallback: str
    activity_slots: int
    workflow_task_slots: int
    cached_workflows: int
    disable_eager_execution: bool
    shutdown_seconds: float


@dataclass(frozen=True)
class RuntimeTemporalDispatcherConfig:
    poll_seconds: float
    reconnect_seconds: float
    reconcile_seconds: float
    reconcile_batch_size: int


@dataclass(frozen=True)
class RuntimeTemporalRenderConfig:
    start_to_close_seconds: float
    schedule_to_close_seconds: float
    heartbeat_seconds: float
    retry_initial_seconds: float
    retry_maximum_seconds: float
    maximum_attempts: int


@dataclass(frozen=True)
class RuntimeTemporalFailureConfig:
    start_to_close_seconds: float
    schedule_to_close_seconds: float
    retry_initial_seconds: float
    retry_maximum_seconds: float
    maximum_attempts: int


@dataclass(frozen=True)
class RuntimeTemporalConfig:
    address: str
    namespace: str
    task_queue: str
    workflow_id_prefix: str
    worker: RuntimeTemporalWorkerConfig
    dispatcher: RuntimeTemporalDispatcherConfig
    render: RuntimeTemporalRenderConfig
    failure: RuntimeTemporalFailureConfig


@dataclass(frozen=True)
class RuntimeBotConfig:
    timezone: str
    notification_poll_seconds: float
    notification_lease_seconds: int
    notification_max_attempts: int
    notification_retry_initial_seconds: float
    notification_retry_backoff_coefficient: float
    notification_retry_max_seconds: float
    notification_renew_divisor: float
    notification_renew_min_seconds: float
    error_max_length: int
    error_excerpt_length: int
    shutdown_seconds: float
    api_timeout_seconds: float
    download_timeout_seconds: float
    noise_min: int
    noise_max: int
    cell_size_min: int
    cell_size_max: int
    result_size_min: int
    result_size_max: int
    regular_user_upload_limit: int


@dataclass(frozen=True)
class RuntimeGenerationConfig:
    insertion_format: str
    alpha_channel: int
    noise_level: int
    cell_size: int
    result_size: int


@dataclass(frozen=True)
class RuntimePathsConfig:
    image_groups: str
    uploads: str
    generated: str
    data_root: str
    compose_env: str
    legacy_bot_env: str
    secrets_root: str
    ui_credentials: str
    ui_password_hash: str
    readable_credentials: str
    mongo_directory: str
    minio_directory: str
    temporal_directory: str
    worker_scratch_directory: str
    bot_scratch_directory: str
    client_temp_directory: str
    rpc_journal_directory: str


@dataclass(frozen=True)
class RuntimeAuthConfig:
    ui_username: str
    ui_password_min_length: int
    ui_password_random_bytes: int
    mongo_password_random_bytes: int
    minio_access_random_bytes: int
    minio_secret_random_bytes: int


@dataclass(frozen=True)
class RuntimeLegacyConfig:
    rabbitmq_url: str
    rabbitmq_requests_queue: str
    rabbitmq_prefetch_count: int
    api_queue_maxsize: int
    api_request_timeout_seconds: float
    api_shutdown_timeout_seconds: float
    rabbitmq_max_retries: int
    rpc_result_ttl_seconds: int
    rpc_result_max_entries: int


@dataclass(frozen=True)
class RuntimeLoggingConfig:
    rotation_bytes: int
    retained_archives: int


@dataclass(frozen=True)
class RuntimeDeploymentConfig:
    temporal_ui_port: int
    minio_console_port: int


@dataclass(frozen=True)
class RuntimeConfig:
    limits: RuntimeLimitsConfig
    memory: RuntimeMemoryConfig
    render: RuntimeRenderConfig
    image: RuntimeImageConfig
    storage: RuntimeStorageConfig
    catalog: RuntimeCatalogConfig
    mongo: RuntimeMongoConfig
    jobs: RuntimeJobsConfig
    temporal: RuntimeTemporalConfig
    bot: RuntimeBotConfig
    generation: RuntimeGenerationConfig
    paths: RuntimePathsConfig
    auth: RuntimeAuthConfig
    legacy: RuntimeLegacyConfig
    logging: RuntimeLoggingConfig
    deployment: RuntimeDeploymentConfig
