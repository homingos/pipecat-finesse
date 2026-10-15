#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Finesse text-to-speech for Pipecat."""

from pipecat_finesse._version import __version__
from pipecat_finesse.tts import FinesseTTSService, FinesseTTSSettings, language_to_finesse_language

__all__ = ["FinesseTTSService", "FinesseTTSSettings", "__version__", "language_to_finesse_language"]
