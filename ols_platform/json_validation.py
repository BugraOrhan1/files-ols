from typing import TypeGuard


def is_json_integer(value: object) -> TypeGuard[int]:
    return type(value) is int
