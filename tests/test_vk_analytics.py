from src.services.vk_analytics import VkAudience, VkCabinet, VkText, banner_copy, build_vk_report, goals_of


def test_goals_prefer_vk_result():
    assert goals_of({"goals": 40, "vk": {"goals": 413}}) == 413
    assert goals_of({"goals": 4}) == 4


def test_banner_copy_reads_vk_textblocks():
    title, body = banner_copy(
        {
            "title_40_vkads": {"text": "Роллы за 30 минут"},
            "text_2000": {"text": "Свежие суши с доставкой"},
            "cta_miniapp_vk": {"text": "Перейти"},
        }
    )
    assert title == "Роллы за 30 минут"
    assert body == "Свежие суши с доставкой"


def test_same_text_is_summed_across_cabinets():
    first = VkCabinet(
        client_id=1,
        name="Курган",
        username="",
        status="active",
        balance=0,
        spend=100,
        shows=10,
        clicks=2,
        goals=3,
        texts=[VkText(7, "Роллы", "за 30 минут", "active", 100, 10, 2, 3)],
        audiences=[VkAudience("Жители", "сегмент", 1200, "2026-01-01", "")],
    )
    second = VkCabinet(
        client_id=2,
        name="Ковров",
        username="",
        status="active",
        balance=0,
        spend=50,
        shows=5,
        clicks=1,
        goals=1,
        texts=[VkText(8, "роллы", "за 30 минут", "deleted", 50, 5, 1, 1)],
    )
    report = build_vk_report([first, second])
    assert len(report["texts"]) == 1
    assert report["texts"][0]["goals"] == 4
    assert report["texts"][0]["title"] == "Роллы"
    assert report["texts"][0]["deleted"] is False
    assert [share["name"] for share in report["texts"][0]["shares"]] == ["Курган", "Ковров"]
    assert report["audiences"][0][1].name == "Жители"
    assert report["total_goals"] == 4
