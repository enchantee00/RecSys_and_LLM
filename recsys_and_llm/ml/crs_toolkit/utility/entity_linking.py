import urllib.parse

from .utils import loadJsonFileFromDataset


class EntityLink:
    entityName2link = None

    def __init__(
        self,
        dataset="redial",
        fileName="entityName2link.json",
        load_json_func=loadJsonFileFromDataset,
    ):
        self.entityName2link = load_json_func(
            dataset, fileName, dataset_org="crs_toolkit"
        )

    def __call__(self, entityName):
        return self.entityName2link.get(
            entityName,
            f"https://www.google.com/search?q={urllib.parse.quote_plus(entityName)}",
        )
