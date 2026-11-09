"""Portable app settings and an unencrypted rclone configuration."""

import configparser
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from .diagnostics import error_summary, logger
from .domain import CloudAccount
from .replicas import validate_remote_name


@dataclass
class Settings:
    # Runtime snapshot from rclone; never persisted as a separate account registry.
    accounts: list[CloudAccount] = field(default_factory=list, repr=False, compare=False)
    enabled_remotes: list[str] = field(default_factory=list)
    download_dir: str = str(Path.home() / "Downloads")
    transfers: int = 2
    retries: int = 2
    cache_ttl: int = 300
    log_level: str = "INFO"
    rclone_config: str = ""


class SettingsStore:
    def __init__(self, directory=None):
        if directory is None:
            compiled = globals().get("__compiled__")
            directory = compiled.containing_dir if compiled else Path(__file__).resolve().parents[2]
        self.directory = Path(directory).absolute()
        if (
            self.directory == Path(self.directory.anchor)
            or self.directory.is_symlink()
            or self.directory.is_junction()
        ):
            raise ValueError("안전한 앱 전용 설정 폴더가 필요합니다.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "settings.json"
        self.config_path = self.directory / "rclone.conf"
        self.external_config = False

    def select_config(self, path):
        if not path:
            return
        selected = Path(path)
        if not selected.is_absolute() or not selected.is_file() or selected.is_symlink():
            raise ValueError("기존 rclone config의 절대 파일 경로가 필요합니다.")
        self.config_path = selected
        self.external_config = True

    def load(self):
        settings = Settings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return settings
        except OSError as error:
            logger.warning("settings read failed error=%s", error_summary(error))
            return settings
        except (ValueError, RecursionError) as error:
            logger.warning("settings JSON invalid error=%s", error_summary(error))
            raw = None
        values = asdict(settings)
        values.pop("accounts")
        clean = {}
        if isinstance(raw, dict):
            for name in values:
                if name not in raw:
                    continue
                value = raw[name]
                if name == "enabled_remotes" and isinstance(value, list):
                    remotes = []
                    for remote in value:
                        try:
                            validate_remote_name(remote)
                        except ValueError:
                            continue
                        remotes.append(remote)
                    value = list(dict.fromkeys(remotes))
                candidate = replace(settings, **{name: value})
                try:
                    self.validate(candidate)
                except ValueError, OSError:
                    continue
                settings = candidate
                clean[name] = value
            if raw.get("rclone_config", "") != settings.rclone_config:
                # A rejected config path must not activate names in the fallback config.
                settings.enabled_remotes = []
        if clean != raw:
            try:
                self.save(settings)
                logger.info("settings normalized keys=%d", len(clean))
            except (ValueError, OSError) as error:
                logger.warning("settings normalization failed error=%s", error_summary(error))
        return settings

    @staticmethod
    def validate(settings):
        if not isinstance(settings.enabled_remotes, list):
            raise ValueError("계정 사용 여부 설정 형식이 잘못되었습니다.")
        for name in settings.enabled_remotes:
            if not isinstance(name, str):
                raise ValueError("계정 사용 여부 설정 형식이 잘못되었습니다.")
            validate_remote_name(name)
        if len(set(settings.enabled_remotes)) != len(settings.enabled_remotes):
            raise ValueError("중복 계정 사용 여부 설정입니다.")
        if type(settings.transfers) is not int or not 1 <= settings.transfers <= 8:
            raise ValueError("동시 전송 수는 1~8입니다.")
        if type(settings.retries) is not int or not 0 <= settings.retries <= 5:
            raise ValueError("재시도 수는 0~5입니다.")
        if type(settings.cache_ttl) is not int or not 0 <= settings.cache_ttl <= 86400:
            raise ValueError("캐시 만료 시간은 0~86400초입니다.")
        if not isinstance(settings.log_level, str) or settings.log_level not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
        }:
            raise ValueError("잘못된 로그 수준입니다.")
        if (
            not isinstance(settings.download_dir, str)
            or not Path(settings.download_dir).is_absolute()
        ):
            raise ValueError("다운로드 폴더는 절대 경로여야 합니다.")
        if not isinstance(settings.rclone_config, str):
            raise ValueError("rclone config는 절대 파일 경로여야 합니다.")
        if settings.rclone_config:
            config = Path(settings.rclone_config)
            if not config.is_absolute() or not config.is_file() or config.is_symlink():
                raise ValueError("기존 rclone config의 절대 파일 경로가 필요합니다.")

    def save(self, settings):
        self.validate(settings)
        temporary = self.directory / ("settings-" + uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                raw = asdict(settings)
                raw.pop("accounts")
                json.dump(raw, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def check_config(self):
        if self.config_path.is_symlink():
            raise ValueError("rclone config는 심볼릭 링크를 사용할 수 없습니다.")
        if not self.external_config and not self.config_path.exists():
            return []
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with self.config_path.open(encoding="utf-8-sig") as stream:
                parser.read_file(stream)
        except configparser.Error, UnicodeError:
            raise ValueError(
                "암호화되지 않은 올바른 rclone config가 필요합니다. 기존 파일을 보존합니다."
            ) from None
        return parser.sections()
