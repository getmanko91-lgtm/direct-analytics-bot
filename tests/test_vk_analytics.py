from src.services.vk_analytics import (
    VkAudience,
    VkCabinet,
    VkCampaign,
    VkText,
    banner_copy,
    build_vk_report,
    campaign_kind,
    campaign_type_totals,
    goals_of,
    is_live_cabinet,
)


def test_campaign_kind_reads_the_name_prefix():
    assert campaign_kind("ЗК / Ящики / мин") == "zk"
    assert campaign_kind("ZK / club") == "zk"
    assert campaign_kind("копия ЗК / Ящики") == "zk"
    assert campaign_kind("Копия_1 Копия ЗК / тест") == "zk"
    assert campaign_kind("ВСТУП / Мин") == "join"
    assert campaign_kind("Вступления / Минималка") == "join"
    assert campaign_kind("копия Вступления / Мин") == "join"
    assert campaign_kind("УНИВЕРС / база") == "senler"
    assert campaign_kind("УНИВЕРС. КИНО") == "senler"
    assert campaign_kind("ПРОДВ / КОНКУРС") == "other"


def test_univers_is_not_counted_as_vk_result():
    cabinet = VkCabinet(
        client_id=1,
        name="Иркутск",
        username="",
        status="active",
        balance=0,
        spend=230,
        shows=20,
        clicks=6,
        goals=16,
        campaigns=[
            VkCampaign(1, "ЗК / гео", "", "active", 100, 10, 2, 5),
            VkCampaign(2, "Вступления / мин", "", "active", 50, 5, 1, 2),
            VkCampaign(3, "УНИВЕРС / база", "", "active", 80, 8, 3, 9),
        ],
    )
    totals = campaign_type_totals([(cabinet, plan) for plan in cabinet.campaigns])
    assert totals["zk"]["goals"] == 5
    assert totals["join"]["goals"] == 2
    assert totals["senler"]["goals"] == 0
    assert totals["senler"]["spend"] == 80


def _cabinet(client_id: int, name: str, status: str, goals: float, spend: float, error: str | None = None) -> VkCabinet:
    return VkCabinet(
        client_id=client_id,
        name=name,
        username="",
        status=status,
        balance=0,
        spend=spend,
        shows=1,
        clicks=1,
        goals=goals,
        error=error,
    )


def test_archived_cabinets_are_excluded():
    live = _cabinet(1, "Пряников Иркутск", "active", 5, 10)
    archived = _cabinet(2, "Тэйк Ит (TakeEat)", "blocked", 100, 500, "VK oauth: 400 invalid_request")
    deleted = _cabinet(3, "Каннам Чикен Иркутск", "deleted", 40, 80)
    report = build_vk_report([live, archived, deleted])
    assert [item.name for item in report["cabinets"]] == ["Пряников Иркутск"]
    assert report["total_goals"] == 5
    assert report["total_spend"] == 10
    assert is_live_cabinet(archived) is False


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
