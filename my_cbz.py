import os

# import zipfile
from pathlib import Path

from cbz.comic import ComicInfo
from cbz.constants import AgeRating, Format, Manga, PageType, YesNo
from cbz.page import PageInfo

import config


def create_cbz(*args, **kwargs):
    # print(f"{args=}")
    index, title, manga_name, save_dir, cbz_dir, path_word = args
    dir_path = config.SETTINGS['download_path']
    save_dir_path = os.path.join(dir_path, save_dir)

    pages = []
    for path in Path(save_dir_path).iterdir():
        # page_type = (
        #     PageType.FRONT_COVER
        #     if i == 0
        #     else (PageType.BACK_COVER if i == len(list(paths)) - 1 else PageType.STORY)
        # )
        page_type = PageType.FRONT_COVER if len(pages) == 0 else PageType.STORY
        pages.append(PageInfo.load(path=path, type=page_type))

    # print(f"{pages=}")
    manga_title = f"{manga_name}-{title}"
    # Create a ComicInfo object with your comic's metadata
    comic = ComicInfo.from_pages(
        pages=pages,
        title=manga_title,
        series=manga_name,
        number=index,
        language_iso='zh',
        format=Format.WEB_COMIC,
        black_white=YesNo.NO,
        manga=Manga.YES,
        age_rating=AgeRating.UNKNOWN,
    )

    # Pack the comic into a CBZ file
    cbz_content = comic.pack()

    # Save the CBZ file
    file_name = f"{manga_title}.cbz"
    cbz_dir = os.path.join(cbz_dir, path_word)
    file_path = os.path.join(cbz_dir, file_name)

    cbz_path = Path(file_path)
    cbz_path.write_bytes(cbz_content)
