"""Pure firmware naming policy for the API Core Worker."""

import re

FIRMWARE_TAG_PATTERN = re.compile(
    r"^(?:Omi_CV1|Omi_DK2|OmiGlass|OpenGlass|Friend)_v[0-9]+(?:\.[0-9]+){1,2}$", re.IGNORECASE
)
DEVICE_PREFIXES = {
    "Omi DevKit 2": "Omi_DK2",
    "Friend DevKit 1": "Friend",
    "Friend": "Friend",
    "OpenGlass": "OpenGlass",
    "Omi CV 1": "Omi_CV1",
    "OMI Glass": "OmiGlass",
    "OmiGlass": "OmiGlass",
    "nrf5340": "Omi_CV1",
}
