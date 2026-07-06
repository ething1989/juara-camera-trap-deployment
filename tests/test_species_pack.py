from pathlib import Path

from juara_station.species_pack import build_species_list_from_pack, write_active_species_list


def test_species_pack_selects_region_metadata_but_only_unions_nearest_cells(tmp_path: Path):
    pack = tmp_path / "pack"
    (pack / "metadata").mkdir(parents=True)
    (pack / "cells").mkdir()
    (pack / "regions").mkdir()
    (pack / "metadata" / "cell_index.csv").write_text(
        "\n".join(
            [
                "cell_id,center_lat,center_lon,lat_min,lat_max,lon_min,lon_max,radius_km_approx,species_count,file",
                "a,-17.5,-57.5,-20,-15,-60,-55,278,2,cells/a.txt",
                "b,-17.5,-52.5,-20,-15,-55,-50,278,2,cells/b.txt",
                "c,-12.5,-57.5,-15,-10,-60,-55,278,2,cells/c.txt",
                "d,-22.5,-57.5,-25,-20,-60,-55,278,2,cells/d.txt",
                "e,42.5,-82.5,40,45,-85,-80,278,2,cells/e.txt",
            ]
        )
        + "\n"
    )
    (pack / "metadata" / "region_index.csv").write_text(
        "\n".join(
            [
                "key,title,description,lat_min,lat_max,lon_min,lon_max,bbox_wraps_dateline,cell_count,species_count,species_file,counts_file",
                "world,World,Global,-90,90,-180,180,False,999,10,regions/world.txt,regions/world_counts.tsv",
                "south_america,South America,Large,-57,13,-82,-34,False,126,4,regions/south_america.txt,regions/south_america_counts.tsv",
                "amazon_rainforest,Amazon Rainforest,Specific,-20,9,-82,-45,False,42,3,regions/amazon_rainforest.txt,regions/amazon_rainforest_counts.tsv",
            ]
        )
        + "\n"
    )
    (pack / "regions" / "amazon_rainforest.txt").write_text("Species one\nSpecies two\n")
    (pack / "regions" / "south_america.txt").write_text("Wrong broad species\n")
    for name, species in {
        "a": "Cell A bird\nSpecies one\n",
        "b": "Cell B bird\n",
        "c": "Cell C bird\n",
        "d": "Cell D bird\n",
        "e": "Cell E bird\n",
    }.items():
        (pack / "cells" / f"{name}.txt").write_text(species)

    species, selection = build_species_list_from_pack(pack, -17.102778, -56.941639)

    assert selection.region_key == "amazon_rainforest"
    assert len(selection.cell_files) == 4
    assert "Species one" in species
    assert "Species two" not in species
    assert "Cell E bird" not in species
    assert "Wrong broad species" not in species


def test_species_pack_writes_active_list_and_metadata(tmp_path: Path):
    pack = tmp_path / "pack"
    (pack / "metadata").mkdir(parents=True)
    (pack / "cells").mkdir()
    (pack / "regions").mkdir()
    (pack / "metadata" / "cell_index.csv").write_text(
        "cell_id,center_lat,center_lon,lat_min,lat_max,lon_min,lon_max,radius_km_approx,species_count,file\n"
        "a,0,0,-2.5,2.5,-2.5,2.5,278,1,cells/a.txt\n"
    )
    (pack / "metadata" / "region_index.csv").write_text(
        "key,title,description,lat_min,lat_max,lon_min,lon_max,bbox_wraps_dateline,cell_count,species_count,species_file,counts_file\n"
    )
    (pack / "cells" / "a.txt").write_text("Only bird\n")
    output = tmp_path / "active.txt"

    selection = write_active_species_list(pack, output, 0.1, 0.1)

    assert output.read_text() == "Only bird\n"
    assert selection.species_count == 1
    assert output.with_suffix(".txt.metadata.json").exists()
