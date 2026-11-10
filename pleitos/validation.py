import re


def normalize_cpf(value):
    return re.sub(r"\D", "", str(value or ""))


def valid_cpf(value):
    cpf = normalize_cpf(value)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    digits = [int(char) for char in cpf]
    for length in (9, 10):
        total = sum(digits[index] * (length + 1 - index) for index in range(length))
        check = (total * 10) % 11
        if check == 10:
            check = 0
        if digits[length] != check:
            return False
    return True


def valid_phone(value):
    return len(re.sub(r"\D", "", str(value or ""))) in (10, 11)
