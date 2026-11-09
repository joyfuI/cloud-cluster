import pytest

from cloud_cluster.gui.rclone_help_ko import translate_auth_text


@pytest.mark.parametrize(
    "source, prefix, preserved",
    [
        (
            'Change current Shared Drive (Team Drive) ID "drive-123"?\n',
            "현재 공유 드라이브",
            '"drive-123"',
        ),
        ('Change current root folder id "root-123"?\n', "현재 루트 폴더", '"root-123"'),
        (
            "rclone's shared Google Photos client_id is being retired and will stop working during 2026.\n"
            "Create your own to avoid interruption: https://example.com/help\n\n"
            "Continue using the shared client_id anyway?",
            "rclone의 공유 Google Photos",
            "https://example.com/help",
        ),
        (
            "Verification code\n\nGo to this URL, authenticate then paste the code here.\n\n"
            "https://example.com/auth?state=unchanged\n",
            "다음 URL에서 로그인한 뒤",
            "https://example.com/auth?state=unchanged",
        ),
        (
            "For this to work, you will need rclone available on a machine that has\n"
            "a web browser available.\n\n"
            "For more help and alternate methods see: https://rclone.org/remote_setup/\n\n"
            "Execute the following on the machine with the web browser (same rclone\n"
            'version recommended):\n\n\trclone authorize "google photos" "encoded-config"\n'
            "\nThen paste the result.\n",
            "웹 브라우저가 있는 컴퓨터에",
            '\trclone authorize "google photos" "encoded-config"',
        ),
        (
            'Drive OK?\n\nFound drive "Work Drive" of type "business"\nURL: https://example.com/drive\n',
            "이 드라이브를 사용할까요?",
            '이름: "Work Drive"\n유형: "business"\nURL: https://example.com/drive',
        ),
        ("Enter the verification code sent to +82-10-0000-0000", "+82-10", "+82-10-0000-0000"),
        (
            "Authentication failed: 401 Unauthorized\n\nTry Again?",
            "인증에 실패했습니다",
            "401 Unauthorized",
        ),
        (
            "Couldn't decode response - try again (make sure you are using a matching version of rclone on both sides: invalid JSON\n",
            "인증 결과를 해석하지 못했습니다",
            "invalid JSON",
        ),
        (
            "Failed to query available drives: /me/drives: first; /me/drive: second\n"
            "Enter the drive ID manually instead",
            "드라이브 목록을 조회하지 못했습니다",
            "/me/drives: first\n/me/drive: second",
        ),
        ("Failed to query available drives: AccessDenied", "드라이브 목록", "AccessDenied"),
        ("Failed to query available sites: AccessDenied", "사이트 목록", "AccessDenied"),
        (
            "Failed to query available site by relative path: AccessDenied",
            "상대 경로로 사이트",
            "AccessDenied",
        ),
        ('Failed to query root for drive "drive-id": AccessDenied', "드라이브", '"drive-id"'),
        (
            'search for "my team" returned no results',
            '"my team" 검색 결과가 없습니다.',
            '"my team"',
        ),
        (
            "The device to use. In standard setup the built-in Jotta device is used,\n"
            "which contains predefined mountpoints for archive, sync etc. All other devices\n"
            "are treated as backup devices by the official Jottacloud client. You may create\n"
            "a new by entering a unique name.",
            "사용할 장치를 선택하세요",
            "Jotta",
        ),
        (
            "The mountpoint to use on the built-in device Jotta.\n"
            "The standard setup is to use the Archive mountpoint. Most other mountpoints\n"
            "have very limited support in rclone and should generally be avoided.",
            "기본 장치 Jotta",
            "Archive",
        ),
        (
            "The mountpoint to use on the non-standard device My PC.\n"
            "You may create a new by entering a unique name.",
            "장치 My PC",
            "My PC",
        ),
    ],
)
def test_auth_text_translates_messages_without_changing_data(source, prefix, preserved):
    translated = translate_auth_text(source)
    assert translated.startswith(prefix)
    assert preserved in translated
    assert translated != source


def test_unknown_auth_text_keeps_original_details():
    source = "New provider response: opaque-id/2026?state=unchanged"
    assert translate_auth_text(source) == source
