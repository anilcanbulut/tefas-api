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


# --- /api/funds/fonYonetimBazliBilgiGetir -------------------------------------
#
# Costs, for every fund, in one request. This is the only endpoint that carries
# them, and they are the one number daily prices cannot tell you: NAVs are
# already net of the management fee, so a fund's published return silently
# blends manager skill with how much the manager charged. The fee separates
# the two.
#
# TEFAS labels the same three columns differently per fund type, and says so in
# the `altbaslik` keys rather than in the field names -- securities funds get
# "yonetim ucreti" (management fee) and a *maximum* total expense ratio, while
# pension funds get "fon isletim gideri" (fund operating expense) and an
# *actual* one. The labels are mapped rather than dropped because they are the
# only thing that distinguishes a cap from a charge.

FEE_FIELDS = {
    "fonKodu": "code",
    "fonUnvan": "title",
    "fonTurAciklama": "category",
    "fonTurKod": "category_code",
    "kurucuKod": "founder_code",
    "tefasDurum": "on_tefas",
    "uygulananYu1Y": "management_fee",
    # "The management fee rate given in the fund rules" -- usually the ceiling
    # the applied fee sits under, but not dependably: some funds state 0 while
    # applying 0.2, so this is reported as what TEFAS calls it rather than
    # relabelled as a cap it does not always bound.
    "fonIcTuzukYu1G": "management_fee_stated",
    "fonTopGiderKesoran": "total_expense_ratio",
    "altbaslik1": "fee_basis",
    "altbaslik2": "fee_ceiling_basis",
    "altbaslik3": "expense_basis",
    "yillikGetiri": "annual_return",
}

#: Fee columns arrive as Turkish-formatted strings ("2,04"), not numbers.
FEE_NUMERIC = ("management_fee", "management_fee_stated", "total_expense_ratio")

#: What TEFAS sends when a fund does not publish a figure. Exchange traded
#: funds use "-" for both fee columns; treating that as text would poison every
#: comparison, and treating it as zero would call the most expensive funds free.
FEE_MISSING = frozenset({"-", "", "N/A"})


def parse_decimal(value):
    """Turkish-formatted number to float. Returns ``None`` when absent.

    "2,04" is two point oh four, and "1.234,5" is one thousand two hundred
    thirty four point five: the separators are the other way round from the
    English convention.
    """
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text in FEE_MISSING:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None
