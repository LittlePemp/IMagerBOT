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
class RuntimeWatermarkConfig:
    font_height_fraction: float
    min_font_height_pixels: int
    max_font_height_pixels: int
    preferred_width_fraction: float
    margin_fraction: float
    min_margin_pixels: int
    padding_pixels: int
    stroke_fraction: float
    outline_pixels: int
    background_opacity: float
    foreground_bgr: tuple[int, ...]
    background_bgr: tuple[int, ...]


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
class RuntimeRetentionConfig:
    enabled: bool
    source_count: int
    result_count: int
    poll_seconds: float
    batch_size: int
    scan_size: int
    lease_seconds: int


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
    bot_admissions_collection: str
    bot_fsm_collection: str
    bot_progress_collection: str


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
class RuntimeTemporalTLSConfig:
    enabled: bool
    root_ca_cert: str
    client_cert: str
    client_key: str
    server_name: str


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
    tls: RuntimeTemporalTLSConfig


@dataclass(frozen=True)
class RuntimeBotConfig:
    timezone: str
    notification_poll_seconds: float
    notification_lease_seconds: int
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
    admission_poll_seconds: float
    admission_lease_seconds: int
    admission_retry_seconds: float
    max_pending_jobs: int
    progress_poll_seconds: float
    progress_batch_size: int
    progress_retention_seconds: int
    source_disk_headroom_bytes: int
    max_concurrent_updates: int
    max_concurrent_uploads: int
    updates_per_second: float
    updates_burst: int
    global_updates_per_second: float
    global_updates_burst: int
    upload_cooldown_seconds: float
    throttle_notice_seconds: float
    throttle_user_capacity: int
    throttle_idle_seconds: float
    ui_page_size: int
    ui_label_max_length: int
    ui_name_max_length: int
    ui_view_token_bytes: int
    ui_view_ttl_seconds: float
    history_repair_poll_seconds: float
    history_repair_lease_seconds: int
    history_repair_retry_initial_seconds: float
    history_repair_retry_max_seconds: float
    history_repair_backoff_coefficient: float
    notification_retry_after_grace_seconds: float
    api_base_url: str
    api_local: bool


@dataclass(frozen=True)
class RuntimePackImportConfig:
    jobs_collection: str
    task_queue: str
    workflow_id_prefix: str
    max_archive_bytes: int
    cloud_max_archive_bytes: int
    max_entries: int
    max_expanded_bytes: int
    max_file_bytes: int
    max_compression_ratio: float
    extract_chunk_bytes: int
    max_path_bytes: int
    max_path_depth: int
    max_central_directory_bytes: int
    allowed_compression_methods: tuple[int, ...]
    ignored_files: tuple[str, ...]
    ignored_directories: tuple[str, ...]
    temp_prefix: str
    archive_filename: str
    extraction_directory: str
    archive_key_template: str
    lease_seconds: int
    heartbeat_seconds: float
    progress_interval_images: int
    activity_timeout_seconds: float
    schedule_timeout_seconds: float
    retry_initial_seconds: float
    retry_max_seconds: float
    maximum_attempts: int
    failure_timeout_seconds: float
    dispatch_retry_seconds: float
    max_pending_per_admin: int
    max_pending_global: int
    recent_limit: int
    download_timeout_seconds: float
    disk_headroom_bytes: int
    bot_api_container_directory: str
    catalog_sync_seconds: float


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
    catalog_import_scratch_directory: str
    bot_api_directory: str
    bot_api_temp_directory: str


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
class RuntimeProductionConfig:
    env_file: str
    data_directory: str
    secrets_directory: str
    postgres_directory: str
    postgres_user: str
    postgres_database: str
    visibility_database: str
    postgres_password_random_bytes: int
    tls_directory: str
    ca_certificate: str
    ca_private_key: str
    server_certificate: str
    server_private_key: str
    client_certificate: str
    client_private_key: str
    postgres_certificate: str
    postgres_private_key: str
    certificate_days: int
    ca_certificate_days: int
    rsa_key_bits: int
    temporal_ui_port: int
    minio_console_port: int
    history_shards: int
    namespace_retention_days: int
    namespace_startup_attempts: int
    namespace_retry_seconds: float


@dataclass(frozen=True)
class RuntimeDeploymentConfig:
    temporal_ui_port: int
    minio_console_port: int
    production: RuntimeProductionConfig


@dataclass(frozen=True)
class RuntimeConfig:
    limits: RuntimeLimitsConfig
    memory: RuntimeMemoryConfig
    render: RuntimeRenderConfig
    watermark: RuntimeWatermarkConfig
    image: RuntimeImageConfig
    storage: RuntimeStorageConfig
    catalog: RuntimeCatalogConfig
    retention: RuntimeRetentionConfig
    mongo: RuntimeMongoConfig
    jobs: RuntimeJobsConfig
    temporal: RuntimeTemporalConfig
    bot: RuntimeBotConfig
    pack_import: RuntimePackImportConfig
    generation: RuntimeGenerationConfig
    paths: RuntimePathsConfig
    auth: RuntimeAuthConfig
    legacy: RuntimeLegacyConfig
    logging: RuntimeLoggingConfig
    deployment: RuntimeDeploymentConfig
