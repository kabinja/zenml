from unittest.mock import patch

import pytest

from zenml.model.model import Model


@pytest.mark.parametrize(
    "version_name,logger",
    [
        ["staging", "info"],
        ["1", "info"],
        [1, "info"],
    ],
    ids=[
        "Pick model by text stage",
        "Pick model by text version number",
        "Pick model by integer version number",
    ],
)
def test_init_warns(version_name, logger):
    with patch(f"zenml.model.model.logger.{logger}") as logger:
        Model(
            name="foo",
            version=version_name,
        )
        logger.assert_called_once()
