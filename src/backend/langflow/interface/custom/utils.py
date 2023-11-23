import re
from types import GenericAlias
from typing import Any


def extract_inner_type(return_type: str) -> str:
    """
    Extracts the inner type from a type hint that is a list.
    """
    if match := re.match(r"list\[(.*)\]", return_type, re.IGNORECASE):
        return match[1]
    return return_type


def extract_inner_type_from_generic_alias(return_type: GenericAlias) -> Any:
    """
    Extracts the inner type from a type hint that is a list.
    """
    if return_type.__origin__ == list:
        return list(return_type.__args__)

    return return_type


def extract_union_types_from_generic_alias(return_type: GenericAlias) -> tuple:
    """
    Extracts the inner type from a type hint that is a Union.
    """
    return return_type.__args__


def extract_union_types(return_type: str) -> list[str]:
    """
    Extracts the inner type from a type hint that is a list.
    """
    # If the return type is a Union, then we need to parse it
    return_type = return_type.replace("Union", "").replace("[", "").replace("]", "")
    return_types = return_type.split(",")
    return_types = [item.strip() for item in return_types]
    return return_types
