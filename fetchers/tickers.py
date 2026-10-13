"""Ticker universe and extraction of ticker mentions from free text."""

import re

# S&P 500 constituents plus popular retail names, ETFs and meme stocks. Single-letter
# tickers are listed for completeness but never match (the regex needs 2-5 letters).
KNOWN_TICKERS: frozenset[str] = frozenset(
    {
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "BRK", "AVGO",
        "LLY", "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ", "ABBV", "WMT",
        "NFLX", "BAC", "CRM", "ORCL", "CVX", "MRK", "KO", "AMD", "PEP", "TMO", "ADBE",
        "LIN", "ACN", "MCD", "CSCO", "ABT", "WFC", "TMUS", "DHR", "GE", "IBM", "PM", "QCOM",
        "TXN", "VZ", "INTU", "CAT", "AMGN", "ISRG", "NOW", "DIS", "CMCSA", "SPGI", "GS",
        "AXP", "UBER", "PFE", "NEE", "RTX", "LOW", "HON", "UNP", "AMAT", "BKNG", "T", "MS",
        "BLK", "PGR", "LMT", "ELV", "TJX", "SYK", "VRTX", "ETN", "C", "BSX", "SCHW", "COP",
        "MDT", "ADP", "PLD", "CB", "MMC", "FI", "LRCX", "REGN", "BMY", "ADI", "PANW",
        "KLAC", "CI", "MU", "ANET", "SBUX", "SO", "DE", "GILD", "MDLZ", "BX", "UPS", "INTC",
        "NKE", "MO", "DUK", "ICE", "SHW", "CME", "CL", "ZTS", "APH", "WM", "MCK", "CDNS",
        "SNPS", "PYPL", "CMG", "TT", "EQIX", "BA", "CVS", "TGT", "USB", "PNC", "MMM", "FCX",
        "ITW", "NOC", "HCA", "GD", "APD", "CSX", "EMR", "ORLY", "MAR", "ECL", "AON", "FDX",
        "PSX", "MCO", "CARR", "NSC", "EOG", "ROP", "WELL", "AJG", "MSI", "COF", "PCAR",
        "SLB", "WMB", "OXY", "KMI", "TFC", "AFL", "AZO", "NXPI", "MPC", "HLT", "TRV", "DLR",
        "GM", "ADSK", "FTNT", "AEP", "SPG", "O", "JCI", "ALL", "PAYX", "D", "SRE", "BK",
        "AMP", "CPRT", "URI", "PSA", "MET", "LHX", "FAST", "KMB", "CCI", "DHI", "GWW", "A",
        "MSCI", "CTAS", "VLO", "MNST", "FIS", "AIG", "KVUE", "ROST", "PRU", "IDXX", "PEG",
        "OTIS", "EW", "NEM", "LEN", "CMI", "HES", "DOW", "CTVA", "IR", "EXC", "KR", "YUM",
        "GEHC", "RSG", "COR", "F", "ACGL", "CNC", "DD", "ODFL", "STZ", "XEL", "VRSK",
        "FANG", "HSY", "EA", "GIS", "MLM", "MCHP", "SYY", "EXR", "ED", "DAL", "BKR", "CSGP",
        "HPQ", "AME", "VMC", "PWR", "DXCM", "GLW", "TRGP", "WAB", "RMD", "FICO", "ON",
        "LULU", "ROK", "XYL", "HIG", "EFX", "KDP", "CBRE", "CDW", "VICI", "MPWR", "DFS",
        "EBAY", "WTW", "ANSS", "TSCO", "FITB", "GPN", "WEC", "AWK", "MTB", "DECK", "IRM",
        "NUE", "CHTR", "PPG", "TTWO", "HAL", "DOV", "BIIB", "STT", "BRO", "WBD", "NDAQ",
        "KHC", "FTV", "EIX", "CAH", "IFF", "HPE", "ETR", "LYB", "TROW", "GRMN", "SBAC",
        "VLTO", "RJF", "WST", "TYL", "ZBH", "EQR", "PHM", "NVR", "HUBB", "LDOS", "DTE",
        "APTV", "ES", "CHD", "SW", "FE", "STE", "ADM", "FSLR", "WY", "STX", "WDC", "BR",
        "PPL", "VTR", "AEE", "ATO", "CINF", "PTC", "WAT", "CNP", "NTRS", "CMS", "CLX",
        "MKC", "DRI", "OMC", "HBAN", "CFG", "ILMN", "COO", "BLDR", "LH", "PKG", "MAS",
        "INVH", "ESS", "J", "TER", "BALL", "EXPD", "ARE", "MAA", "SNA", "LVS", "IP", "AVY",
        "DGX", "HOLX", "LUV", "ULTA", "GPC", "SWKS", "UAL", "ALGN", "DPZ", "TXT", "CF",
        "AKAM", "JBHT", "EL", "TRMB", "BAX", "APA", "PODD", "NRG", "JBL", "CAG", "NI",
        "AMCR", "TPR", "EVRG", "CPT", "LNT", "KEY", "L", "HST", "REG", "FDS", "EPAM",
        "FFIV", "JNPR", "PNR", "UDR", "IEX", "TAP", "CHRW", "POOL", "MOS", "ALLE", "SWK",
        "CRL", "DVA", "EMN", "AES", "BG", "HSIC", "LKQ", "AIZ", "GL", "NWSA", "NWS", "MGM",
        "CPB", "HAS", "WYNN", "PNW", "NCLH", "CZR", "FOXA", "FOX", "BWA", "RL", "IPG",
        "MTCH", "TECH", "KMX", "PARA", "AOS", "MHK", "FMC", "BBWI", "BEN", "IVZ", "AAL",
        "BIO", "DAY", "CE", "ETSY", "GNRC", "NDSN", "TFX", "WBA", "VFC", "ZION", "PAYC",
        "CTLT", "QRVO", "RHI", "MKTX", "HII", "GME", "AMC", "PLTR", "SOFI", "HOOD", "SNDK",
        "COIN", "MSTR", "RIVN", "LCID", "NIO", "XPEV", "LI", "BB", "NOK", "BBBY", "SPCE",
        "CHPT", "PLUG", "FCEL", "RIOT", "MARA", "CLSK", "HUT", "BITF", "CAN", "WISH",
        "CLOV", "OPEN", "UPST", "AFRM", "SQ", "SHOP", "SNOW", "DDOG", "NET", "CRWD", "ZS",
        "OKTA", "MDB", "TWLO", "ROKU", "PINS", "SNAP", "RBLX", "U", "DKNG", "PENN", "CVNA",
        "CHWY", "W", "RKT", "LMND", "ASTS", "RKLB", "IONQ", "RGTI", "QBTS", "SOUN", "BBAI",
        "SMCI", "ARM", "TSM", "ASML", "BABA", "JD", "PDD", "BIDU", "SE", "MELI", "NU",
        "DASH", "ABNB", "LYFT", "TTD", "APP", "DUOL", "RDDT", "CART", "CAVA", "ONON",
        "BROS", "CELH", "ELF", "WOOF", "TLRY", "CGC", "SNDL", "ACB", "CRON", "NVAX", "MRNA",
        "BNTX", "CRSP", "BEAM", "NTLA", "EDIT", "VKTX", "HIMS", "OSCR", "ENVX", "QS",
        "JOBY", "ACHR", "LILM", "EH", "BLNK", "RUN", "ENPH", "SEDG", "NOVA", "SPWR", "NKLA",
        "FSR", "GOEV", "WKHS", "RIDE", "FFIE", "MULN", "TTOO", "SAVA", "ATER", "BYND",
        "DNA", "PTON", "SKLZ", "GRAB", "CCL", "RCL", "JETS", "SPY", "QQQ", "IWM", "DIA",
        "VOO", "VTI", "ARKK", "TQQQ", "SQQQ", "SOXL", "SOXS", "UVXY", "VXX", "SPXL", "TLT",
        "GLD", "SLV", "USO", "UNG", "XLF", "XLE", "XLK", "XLV", "XLY", "XLP", "XLI", "XLU",
        "XLB", "XLRE", "XLC", "SMH", "SOXX", "IBIT", "GBTC", "ETHE", "BITO", "MSOS", "HYG",
        "LQD", "EEM", "EFA", "FXI", "KWEB", "BITX", "TNA", "TZA", "LABU", "SPXS", "SDOW",
        "UPRO", "NVDL", "TSLL", "CONL", "MSTU", "AVGX", "ENVA", "SIRI", "PBR", "VALE",
        "ITUB", "BP", "SHEL", "TTE", "RIO", "BHP", "CNQ", "SU", "ENB", "TRP", "CP", "CNI",
        "BNS", "RY", "TD", "BMO", "CM", "MFC", "SLF", "MRVL", "ALAB", "CRDO", "CLS", "VRT",
        "CEG", "VST", "OKLO", "SMR", "NNE", "LEU", "CCJ", "UEC", "UUUU", "DNN", "NXE",
        "ASTR", "LUNR", "PL", "BKSY", "RDW", "SATS", "GSAT", "IRDM", "VSAT", "IBKR", "LPLA",
        "SF", "EVR", "HLI", "PJT", "MKL", "WRB", "ERIE", "RLI", "AXON", "TDG", "HWM", "LII",
        "WSM", "EXE", "CPAY", "TPL", "DELL", "GDDY", "NTAP", "FLEX", "TKO", "LYV", "EXPE",
        "TRIP", "PCG", "SMMT", "RVMD", "INSM", "ALNY", "BMRN", "SRPT", "NBIX", "UTHR",
        "EXEL", "INCY", "JAZZ", "NTRA", "TEM", "ACLX", "CRNX", "RARE",
    }
)

# Tickers that are also common all-caps words; only counted when written as a cashtag.
STOPWORDS: frozenset[str] = frozenset(
    {
        "ALL", "ARE", "CAN", "FOR", "NOW", "IT", "ON", "ONE", "OUT", "SO", "BE", "GO",
        "BIG", "DD", "YOLO", "CEO", "USA", "EV", "AI", "A", "AM", "AN", "AT", "BY", "DO",
        "HAS", "HE", "HOLD", "LOW", "MAY", "NEW", "OR", "PM", "REAL", "RUN", "SEE", "TOP",
        "TWO", "WELL", "WAY", "FAST", "GOOD", "LOVE", "OPEN", "PLAY", "EDIT", "ANY", "ELF",
        "COST", "GM", "LI", "FUN", "CASH", "TECH", "LIFE", "DAY", "FREE", "SAFE", "PEAK",
        "POOL", "NEXT", "TRUE", "FLEX", "NU", "CF", "IP", "FI", "ES", "EL", "ED",
    }
)

_TICKER_RE = re.compile(r"\b\$?([A-Z]{2,5})\b")
_CASHTAG_RE = re.compile(r"\$([A-Z]{2,5})\b")


def extract_tickers(text: str) -> set[str]:
    """Known tickers mentioned in text; STOPWORDS only count when written as $CASHTAG."""
    if not text:
        return set()
    cashtags = set(_CASHTAG_RE.findall(text)) & KNOWN_TICKERS
    plain = {t for t in _TICKER_RE.findall(text) if t in KNOWN_TICKERS and t not in STOPWORDS}
    return plain | cashtags
