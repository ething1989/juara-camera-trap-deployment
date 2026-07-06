from pathlib import Path

from juara_station.taxonomy import resolve_taxon


def test_clements_taxonomy_resolves_non_heuristic_species():
    taxon = resolve_taxon("Plumbeous ibis")

    assert taxon.genus == "Theristicus"
    assert taxon.family == "Threskiornithidae"
    assert taxon.order == "Pelecaniformes"


def test_clements_taxonomy_resolves_from_species_list_scientific_name(tmp_path: Path):
    species_list = tmp_path / "species.txt"
    species_list.write_text("Anodorhynchus hyacinthinus_Hyacinth macaw\n")

    taxon = resolve_taxon("Hyacinth macaw", species_list_path=str(species_list))

    assert taxon.genus == "Anodorhynchus"
    assert taxon.family == "Psittacidae"
    assert taxon.order == "Psittaciformes"
