"""金额工具：整数分与展示文本之间的唯一转换点。

铁律：金额的加减乘除全部在整数「分」上完成，本模块不允许出现浮点
参与的金额运算。显示用的元字符串由整数除法和取余拼出，不经 float。
"""


def fen_to_yuan(fen: int, *, thousands: bool = True) -> str:
    """1423 -> '14.23'；负数保留符号。"""
    fen = int(fen)
    sign = "-" if fen < 0 else ""
    value = abs(fen)
    whole, frac = divmod(value, 100)
    body = f"{whole:,}" if thousands else str(whole)
    return f"{sign}{body}.{frac:02d}"


def yuan_mark(fen: int, *, thousands: bool = True) -> str:
    """带货币符号的展示文本：'¥14.23' / '-¥14.23'。"""
    fen = int(fen)
    if fen < 0:
        return "−¥" + fen_to_yuan(-fen, thousands=thousands)
    return "¥" + fen_to_yuan(fen, thousands=thousands)


def signed_yuan_mark(fen: int, *, thousands: bool = True) -> str:
    """带正负号的展示文本，用于差额对比。"""
    fen = int(fen)
    if fen > 0:
        return "+¥" + fen_to_yuan(fen, thousands=thousands)
    return yuan_mark(fen, thousands=thousands)


def round_half_up_div(numerator: int, denominator: int) -> int:
    """整数四舍五入除法，仅用于非负整数。"""
    if numerator < 0:
        raise ValueError("round_half_up_div 仅接受非负整数")
    return (numerator * 2 + denominator) // (denominator * 2)


def fee_from_bp(amount_fen: int, rate_bp: int) -> int:
    """按基点费率算手续费：100 元 60bp -> 60 分。整数运算，四舍五入。"""
    return round_half_up_div(amount_fen * rate_bp, 10000)
