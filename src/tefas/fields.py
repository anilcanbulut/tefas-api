"""Field maps between TEFAS's Turkish API keys and English column names.

Canonical column names in this package are the *English* names below. Any key
TEFAS returns that is not in a map is passed through under its raw API name
rather than dropped, so a new column upstream shows up as data instead of
silently disappearing.
"""

# --- /api/funds/fonGnlBlgSiraliGetir -----------------------------------------

INFO_FIELDS = {
    "fonKodu": "code",
    "fonUnvan": "title",
    "tarih": "date",
    "fiyat": "price",
    "tedPaySayisi": "number_of_shares",
    "kisiSayisi": "number_of_investors",
    "portfoyBuyukluk": "market_cap",
    "borsaBultenFiyat": "exchange_bulletin_price",
}

# --- /api/funds/fonGetiriBazliBilgiGetir --------------------------------------

RETURN_FIELDS = {
    "fonKodu": "code",
    "fonUnvan": "title",
    "fonTurAciklama": "category",
    "tefasDurum": "tradable_on_tefas",
    "getiri1a": "return_1m",
    "getiri3a": "return_3m",
    "getiri6a": "return_6m",
    "getiri1y": "return_1y",
    "getiriyb": "return_ytd",
    "getiri3y": "return_3y",
    "getiri5y": "return_5y",
    "getiriOrani": "return_rate",
    "riskDegeri": "risk_value",
}

# --- /api/funds/dagilimSiraliGetirT -------------------------------------------
#
# Portfolio allocation, one column per instrument class, values are percentages.
# These English names follow the mapping TEFAS itself used on the legacy
# endpoint, so they stay comparable with data collected before the 2026 API
# migration. Note DB and DT are easy to transpose: DB is *fx payable bills*,
# DT is *government bond*.

BREAKDOWN_FIELDS = {
    "fonKodu": "code",
    "fonUnvan": "title",
    "tarih": "date",
    "bb": "bank_bills",
    "byf": "exchange_traded_fund",
    "d": "other",
    "db": "fx_payable_bills",
    "dt": "government_bond",
    "eut": "eurobonds",
    "fb": "commercial_paper",
    "fkb": "fund_participation_certificate",
    "gas": "real_estate_certificate",
    "gsykb": "venture_capital_investment_fund_participation",
    "gykb": "real_estate_investment_fund_participation",
    "hb": "treasury_bill",
    "hs": "stock",
    "kba": "government_bonds_and_bills_fx",
    "kh": "participation_account",
    "khau": "participation_account_au",
    "khd": "participation_account_d",
    "khtl": "participation_account_tl",
    "kks": "government_lease_certificates",
    "kksd": "government_lease_certificates_d",
    "kkstl": "government_lease_certificates_tl",
    "kksyd": "government_lease_certificates_foreign",
    "km": "precious_metals",
    "kmbyf": "precious_metals_byf",
    "kmkba": "precious_metals_kba",
    "kmkks": "precious_metals_kks",
    "osks": "private_sector_lease_certificates",
    "ost": "private_sector_bond",
    "r": "repo",
    "t": "derivatives",
    "tpp": "tmm",
    "tr": "reverse_repo",
    "vdm": "asset_backed_securities",
    "vm": "term_deposit",
    "vmau": "term_deposit_au",
    "vmd": "term_deposit_d",
    "vmtl": "term_deposit_tl",
    "yba": "foreign_debt_instruments",
    "ybkb": "foreign_domestic_debt_instruments",
    "ybosb": "foreign_private_sector_debt_instruments",
    "ybyf": "foreign_exchange_traded_funds",
    "yhs": "foreign_equity",
    "ymk": "foreign_securities",
    "yyf": "foreign_investment_fund_participation_shares",
}

#: Allocation keys the 2026 API added that have no established English name.
#: They are returned under these raw codes rather than being guessed at or
#: dropped. Documented here so their presence is deliberate, not an oversight.
UNMAPPED_BREAKDOWN_KEYS = frozenset(
    {
        "bpp",
        "btaa",
        "btas",
        "dot",
        "gsyy",
        "gyy",
        "kibd",
        "oksyd",
        "osdb",
        "vint",
        "bilFiyat",
    }
)

# --- /api/funds/fonBilgiGetir -------------------------------------------------
#
# Per-fund summary card behind the fund detail page. Carries three things no
# other endpoint exposes: where the fund ranks inside its category, how many
# funds it is ranked against, and its share of the whole market.

PROFILE_FIELDS = {
    "fonKodu": "code",
    "fonUnvan": "title",
    "sonFiyat": "last_price",
    "gunlukGetiri": "daily_return",
    "payAdet": "number_of_shares",
    "portBuyukluk": "market_cap",
    "fonKategori": "category",
    "kategoriDerece": "category_rank",
    "kategoriFonSay": "category_fund_count",
    "yatirimciSayi": "number_of_investors",
    "pazarPayi": "market_share",
}

#: Fund types accepted by ``fonTipi``.
KINDS = ("YAT", "EMK", "BYF", "GYF", "GSYF")

KIND_LABELS = {
    "YAT": "Securities mutual funds",
    "EMK": "Pension funds",
    "BYF": "Exchange traded funds",
    "GYF": "Real estate investment funds",
    "GSYF": "Venture capital investment funds",
}


def rename(row, field_map):
    """Rename one API row, passing unknown keys through untouched."""
    return {field_map.get(key, key): value for key, value in row.items()}
