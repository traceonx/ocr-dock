from dataclasses import dataclass
import os
from pathlib import Path
import secrets


def integer(name, default, lower, upper):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} must be an integer') from None
    if not lower <= value <= upper:
        raise ValueError(f'{name} must be between {lower} and {upper}')
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path('/app/data')
    api_key: str = ''
    max_image_bytes: int = 2 * 1024 * 1024
    max_pixels: int = 4_000_000
    concurrency: int = 1
    max_queue: int = 4
    queue_wait_seconds: int = 10
    rate_per_minute: int = 60
    ort_threads: int = 2
    cors_origins: tuple[str, ...] = ()

    @property
    def max_body_bytes(self):
        # Base64 overhead plus bounded JSON/form metadata.
        return ((self.max_image_bytes + 2) // 3) * 4 + 65536

    @classmethod
    def from_env(cls):
        origins = tuple(s.strip() for s in os.environ.get('CORS_ORIGINS', '').split(',') if s.strip())
        if '*' in origins:
            raise ValueError('CORS_ORIGINS requires explicit origins, not *')
        return cls(
            data_dir=Path(os.environ.get('DATA_DIR', './data')).resolve(),
            api_key=os.environ.get('API_KEY', '').strip(),
            max_image_bytes=integer('MAX_IMAGE_BYTES', 2097152, 1024, 16 * 1024 * 1024),
            max_pixels=integer('MAX_PIXELS', 4000000, 1024, 16000000),
            concurrency=integer('MAX_CONCURRENCY', 1, 1, 4),
            max_queue=integer('MAX_QUEUE', 4, 0, 32),
            queue_wait_seconds=integer('QUEUE_WAIT_SECONDS', 10, 1, 60),
            rate_per_minute=integer('RATE_LIMIT_PER_MINUTE', 60, 0, 100000),
            ort_threads=integer('ORT_THREADS', 2, 1, 16),
            cors_origins=origins,
        )


def load_api_key(settings):
    if settings.api_key:
        key = settings.api_key
    else:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = settings.data_dir / 'api_key'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as stream:
                key = stream.read(4097).strip()
        else:
            key = secrets.token_urlsafe(32)
            with os.fdopen(fd, 'w') as stream:
                stream.write(key + '\n')
    if len(key) < 16 or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError('API key must contain 16–4096 characters without whitespace')
    return key
