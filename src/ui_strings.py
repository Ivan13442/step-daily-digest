from typing import Dict

_UI_STRINGS = {
    "ru": {
        "group_other": "Other",  # пока так, чтобы совпадало с шаблоном текста
    },
    "en": {
        "group_other": "Other",
    },
}


def get_ui_strings(lang: str = "ru") -> Dict[str, str]:
    return _UI_STRINGS.get(lang, _UI_STRINGS["ru"])
