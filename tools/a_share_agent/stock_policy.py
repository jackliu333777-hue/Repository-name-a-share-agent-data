"""Stock-code eligibility rules shared by dashboard features."""

RESTRICTED_STOCK_PREFIXES = ("300", "301", "302", "688", "689")


def code_digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def is_restricted_stock_code(value):
    code = code_digits(value)
    return len(code) == 6 and code.startswith(RESTRICTED_STOCK_PREFIXES)


def is_allowed_stock_code(value):
    code = code_digits(value)
    return not (len(code) == 6 and is_restricted_stock_code(code))

