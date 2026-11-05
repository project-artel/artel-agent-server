"""Suite-wide setup.

The model catalog normally comes from the orchestration server. Tests read
`tests/fixtures/model_catalog.json` instead, which mirrors the rows the
orchestration migration seeds, so no test reaches the network for it. It is
installed at import time rather than in a fixture because some test modules
build pydantic models that check model ids while they are being collected.
"""

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from app.llm.catalog import install_model_catalog, load_model_catalog_file

MODEL_CATALOG_FIXTURE = Path(__file__).parent / "fixtures" / "model_catalog.json"
FIXTURE_MODEL_CATALOG = load_model_catalog_file(MODEL_CATALOG_FIXTURE)

install_model_catalog(FIXTURE_MODEL_CATALOG)


def install_catalog_with_enabled(*model_ids: str) -> None:
    """Installs the fixture catalog with `model_ids` switched on.

    The two Bedrock rows are seeded off, so a test that sends one through a request
    which refuses disabled models has to turn it on first. The autouse fixture
    below restores the fixture catalog afterwards.
    """
    install_model_catalog(
        MappingProxyType(
            {
                model_id: replace(spec, enabled=True) if model_id in model_ids else spec
                for model_id, spec in FIXTURE_MODEL_CATALOG.items()
            }
        )
    )


@pytest.fixture(autouse=True)
def _fixture_model_catalog():
    """Puts the fixture catalog back after a test that installed its own."""
    yield
    install_model_catalog(FIXTURE_MODEL_CATALOG)
