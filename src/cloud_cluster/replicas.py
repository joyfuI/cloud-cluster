"""Conservative, deterministic policies shared by browsing and mutations."""

import re
import unicodedata
from collections import Counter
from fractions import Fraction

from .domain import (
    BackendCapabilities,
    CloudAccount,
    EntryAssessment,
    LogicalEntry,
    QuotaSnapshot,
    Replica,
    Status,
)

STRONG_HASHES = frozenset({"sha256", "sha1", "md5", "dropbox", "blake3", "whirlpool"})
RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
# shortcut: Match provider paths by name; update verified rules when defaults change.
PROVIDER_EXCLUDED_PATHS = {"pikpak": ("My Pack",)}


def path_key(path: str) -> str:
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in path.split("/"))


def path_keys_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def is_excluded(account: CloudAccount, path: str) -> bool:
    roots = PROVIDER_EXCLUDED_PATHS.get(account.provider, ())
    if not roots:
        return False
    key = path_key(account.root + "/" + path if account.root else path)
    return any(key == root or key.startswith(root + "/") for root in map(path_key, roots))


def validate_remote_name(name: str) -> str:
    # Match rclone's fs/fspath.configNameRe without backtracking on long invalid names.
    if not isinstance(name, str) or not re.fullmatch(r"[\w.+@]++(?:[ -]+?[\w.+@-]++)*+", name):
        raise ValueError(
            "리모트 이름에는 한글 등 문자와 숫자, 기호(_, -, ., +, @), 공백만 사용할 수 있습니다. "
            "하이픈이나 공백으로 시작할 수 없으며, 공백으로 끝날 수도 없습니다. "
            "이름 끝에만 붙인 하이픈 한 개도 사용할 수 없습니다."
        )
    return name


def validate_path(path: str, *, allow_root: bool = False, names: bool = True) -> str:
    if not path and allow_root:
        return path
    if not path or path.startswith(("/", "\\")) or "\\" in path or ":" in path:
        raise ValueError("관리 루트 안의 상대 경로만 사용할 수 있습니다.")
    for part in path.split("/"):
        if part in {"", ".", ".."}:
            raise ValueError("빈 경로 또는 . / .. 경로는 사용할 수 없습니다.")
        if names:
            if any(ord(c) < 32 or c in '<>:"/\\|?*' for c in part):
                raise ValueError("Windows에서 허용하지 않는 문자가 있습니다.")
            if part.endswith((" ", ".")) or RESERVED.match(part):
                raise ValueError("예약 이름을 쓰거나 이름을 공백 또는 마침표로 끝낼 수 없습니다.")
            if len(part.encode("utf-16-le")) // 2 > 255:
                raise ValueError("파일 이름이 너무 깁니다.")
    return path


def compare(left: Replica, right: Replica) -> str:
    if left.is_dir != right.is_dir:
        return "different"
    if left.size is not None and right.size is not None and left.size != right.size:
        return "different"
    common = STRONG_HASHES & left.hashes.keys() & right.hashes.keys()
    common = {key for key in common if left.hashes[key] and right.hashes[key]}
    if any(left.hashes[key].lower() != right.hashes[key].lower() for key in common):
        return "different"
    verified = False
    if left.verified_sha256 or right.verified_sha256:
        left_sha = left.verified_sha256 or left.hashes.get("sha256")
        right_sha = right.verified_sha256 or right.hashes.get("sha256")
        verified = bool(left_sha and right_sha)
        if verified and left_sha.lower() != right_sha.lower():
            return "different"
    if (common or verified) and left.size is not None and right.size is not None:
        return "same"
    return "unknown"


def assess(
    replicas: tuple[Replica, ...], *, complete: bool, expected_dirs: set[str]
) -> EntryAssessment:
    issues = []
    count = len({replica.account_id for replica in replicas})
    names = {replica.path for replica in replicas}
    if len(names) > 1:
        issues.append("대소문자 또는 유니코드 표기가 다른 이름의 충돌")
    if max(Counter(replica.account_id for replica in replicas).values(), default=0) > 1:
        issues.append("같은 계정에 중복 객체 존재")
    if len({replica.is_dir for replica in replicas}) > 1:
        issues.append("파일과 폴더의 이름 충돌")
    for path in names:
        try:
            validate_path(path)
        except ValueError, UnicodeError:
            issues.append("Windows 경로 규칙 위반")
            break
    if issues:
        return EntryAssessment(Status.CONFLICT, tuple(issues), count, complete)
    if not complete:
        issues.append("일부 계정 미확인")
    if all(replica.is_dir for replica in replicas):
        missing = expected_dirs - {replica.account_id for replica in replicas}
        if missing:
            issues.append("일부 계정에 디렉터리 없음")
        status = (
            Status.UNKNOWN if not complete else (Status.DIRECTORY if missing else Status.HEALTHY)
        )
        return EntryAssessment(status, tuple(issues), count, complete, "디렉터리")
    comparisons = [compare(replicas[0], item) for item in replicas[1:]]
    # Compare every pair: a replica may expose hashes the first one lacks.
    different = "different" in comparisons or any(
        compare(a, b) == "different"
        for i, a in enumerate(replicas[1:], 1)
        for b in replicas[i + 1 :]
    )
    if different:
        return EntryAssessment(Status.CONFLICT, ("복제본 내용 상이", *issues), count, complete)
    if not complete:
        return EntryAssessment(Status.UNKNOWN, tuple(issues), count, False)
    if count == 1:
        return EntryAssessment(Status.UNDER, ("복제본 1개 확인",), count, True)
    if any(value != "same" for value in comparisons):
        return EntryAssessment(Status.UNVERIFIED, ("비교 가능한 해시 부족",), count, True)
    return EntryAssessment(
        Status.OVER if count > 2 else Status.HEALTHY,
        (),
        count,
        True,
        "정밀 검증 SHA-256" if any(r.verified_sha256 for r in replicas) else "내용 해시",
    )


def merge(
    listings, capabilities: dict[str, BackendCapabilities], accounts=()
) -> tuple[LogicalEntry, ...]:
    grouped: dict[str, list[Replica]] = {}
    excluded_accounts = {a.id: a for a in accounts if a.provider in PROVIDER_EXCLUDED_PATHS}
    complete = all(item.state in {"ok", "absent"} for item in listings)
    for listing in listings:
        account = excluded_accounts.get(listing.account_id)
        for replica in listing.replicas:
            if account and is_excluded(account, replica.path):
                continue
            grouped.setdefault(path_key(replica.path), []).append(replica)
    expected = {
        item.account_id
        for item in listings
        if capabilities.get(item.account_id, BackendCapabilities()).empty_directories
    }
    entries = []
    current_ids = {item.account_id for item in listings if item.state == "ok"}
    for key, items in grouped.items():
        replicas = tuple(
            sorted(items, key=lambda item: (item.account_id, item.path, item.object_id))
        )
        current = tuple(item for item in replicas if item.account_id in current_ids)
        ignored = (
            {a.id for a in excluded_accounts.values() if is_excluded(a, replicas[0].path)}
            if excluded_accounts
            else ()
        )
        entry_complete = (
            all(
                item.state in {"ok", "absent"}
                for item in listings
                if item.account_id not in ignored
            )
            if ignored
            else complete
        )
        assessment = (
            assess(
                current,
                complete=entry_complete,
                expected_dirs=expected - ignored if ignored else expected,
            )
            if current
            else EntryAssessment(Status.UNKNOWN, ("이전 조회 결과만 존재",), 0, False)
        )
        entries.append(LogicalEntry(key, replicas[0].path, replicas, assessment))
    return tuple(sorted(entries, key=lambda item: (not item.is_dir, item.key)))


def effective_capacity(quotas) -> QuotaSnapshot:
    quotas = tuple(quotas)
    if len(quotas) < 2:
        return QuotaSnapshot(0, 0)

    def valid(value):
        return type(value) is int and value >= 0

    totals = [q.total for q in quotas]
    total = None
    if all(valid(value) for value in totals):
        total = min(sum(totals) // 2, sum(totals) - max(totals))
    if not all(
        valid(q.total) and valid(q.free) and valid(q.reserved) and q.free <= q.total for q in quotas
    ):
        return QuotaSnapshot(total, None)

    levels = {}
    for quota in quotas:
        free = max(0, quota.free - quota.reserved)
        if free:
            levels.setdefault(Fraction(free, quota.total), []).append(quota.total)
    groups = [[level, levels[level]] for level in sorted(levels, reverse=True)]
    available = Fraction(0)
    # Follow the two highest free/total ratios continuously, merging only at ratio crossings.
    # shortcut: Ignore file/staging granularity; revise for estimates of specific upload sizes.
    while groups:
        level, weights = groups[0]
        if len(weights) > 1:
            largest, combined = max(weights), sum(weights)
            if largest * 2 > combined:
                rest = list(weights)
                rest.remove(largest)
                groups[:1] = [[level, [largest]], [level, rest]]
            else:
                lower = groups[1][0] if len(groups) > 1 else Fraction(0)
                available += (level - lower) * combined / 2
                if len(groups) == 1:
                    break
                groups[1][1].extend(weights)
                del groups[0]
                continue
        if len(groups) == 1:
            break
        first, second = groups[0], groups[1]
        first_weight, second_weight = first[1][0], sum(second[1])
        lower = groups[2][0] if len(groups) > 2 else Fraction(0)
        descend = (second[0] - lower) * second_weight
        rate = Fraction(1, first_weight) - Fraction(1, second_weight)
        catch = (first[0] - second[0]) / rate if rate > 0 else None
        if catch is not None and catch <= descend:
            available += catch
            groups[:2] = [[first[0] - catch / first_weight, first[1] + second[1]]]
        else:
            available += descend
            first[0] -= descend / first_weight
            if len(groups) == 2:
                break
            groups[2][1].extend(second[1])
            del groups[1]
    return QuotaSnapshot(total, int(available))


def choose_accounts(
    accounts: list[CloudAccount],
    quotas: dict[str, QuotaSnapshot],
    size: int,
    *,
    capabilities=None,
    existing=(),
    required: int = 2,
    include_alternates: bool = False,
) -> tuple[CloudAccount, ...]:
    if size < 0:
        raise ValueError("파일 크기를 확인할 수 없습니다.")
    capabilities = capabilities or {}
    candidates = []
    for account in accounts:
        cap = capabilities.get(account.id, BackendCapabilities())
        quota = quotas.get(account.id, QuotaSnapshot())
        if (
            not account.enabled
            or account.readonly
            or (cap.max_file_size is not None and size > cap.max_file_size)
        ):
            continue
        free = None if quota.free is None else quota.free - quota.reserved - size
        if free is not None and free < 0:
            continue
        if quota.total is not None and quota.total > 0 and free is not None:
            rank = (0, -free / quota.total, account.id)
        elif free is not None:
            rank = (1, -free, account.id)
        else:
            rank = (2, 0, account.id)
        candidates.append((account, rank))
    candidates.sort(key=lambda item: (item[0].id not in existing, item[1]))
    if len(candidates) < required:
        raise ValueError(f"사용 가능한 서로 다른 저장 계정 {required}개가 필요합니다.")
    return tuple(account for account, _ in (candidates if include_alternates else candidates[:required]))
