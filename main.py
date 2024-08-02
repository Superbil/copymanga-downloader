import argparse
import csv
import json
import os
import queue
import string
import sys
import threading
import time

import requests as requests
import tomlkit
from rich import print
from rich.console import Console
from rich.progress import track
from rich.prompt import IntPrompt, Prompt

import config
from epub import epub_transformerhelper
from function import api_restriction, img_api_restriction
from login import login, login_information_builder
from my_cbz import create_cbz
from settings import change_settings, load_settings, save_settings, set_settings

console = Console(color_system='256', style=None)

UPDATE_LIST = []


# API限制相关


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--MangaPath',
        help='漫画的全拼，https://copymanga.site/comic/这部分',
    )
    parser.add_argument(
        '--MangaGroup',
        help='漫画的分组Path_Word，默认为default',
        default='default',
    )

    parser.add_argument(
        '--Url',
        help='copymanga的域名,如使用copymanga.site，那就输入site(默认为site)',
        default="site",
    )

    parser.add_argument('--Output', help='输出文件夹')

    # todo 此功能暂时不在开发维护列表内，以后会随缘更新此功能
    parser.add_argument(
        '--subscribe',
        help='是否切换到自动更新订阅模式(1/0，默认关闭(0))',
        default="0",
    )

    parser.add_argument(
        '--UseWebp',
        help='是否使用Webp(1/0，默认开启(1))',
        default="1",
    )

    parser.add_argument(
        '--UseOSCdn',
        help='是否使用海外cdn(1/0，默认关闭(0))',
        default="0",
    )

    parser.add_argument('--MangaStart', help='漫画开始下载话(如果想全部下载请输入0)')

    parser.add_argument(
        '--MangaEnd',
        help='漫画结束下载话(如果只想下载一话请与MangaStart相同,如果想全部下载请输入0)',
    )

    parser.add_argument('--Proxy', help='设置代理')

    return parser.parse_args()


# 命令行参数全局化

ARGS = parse_args()


# 命令行模式
def command_mode():
    if ARGS.UseOSCdn or ARGS.UseWebp:
        config.API_HEADER['use_oversea_cdn'] = ARGS.UseOSCdn
        config.API_HEADER['use_webp'] = ARGS.UseWebp
    if ARGS.Proxy:
        config.PROXIES = {
            "http": ARGS.Proxy,
            "https": ARGS.Proxy,
        }
    if ARGS.Output:
        config.SETTINGS['download_path'] = ARGS.Output
    manga_chapter_json = manga_chapter(ARGS.MangaPath, ARGS.MangaGroup)
    chapter_allocation(manga_chapter_json)
    print(f"[bold green][:white_check_mark: ]漫画已经🔻完成！[/]")


# 正常模式


def welcome():
    choice_manga_path_word = None
    want_to = int(
        Prompt.ask(
            "您是想搜索还是查看您的收藏？[italic yellow](0:导出收藏,1:搜索,2:收藏,3:添加半自动更新,9:修改设置)[/]",
            choices=["0", "1", "2", "3", "9"],
            default="1",
        ),
    )
    if want_to == 0:
        collect_expect()
        return
    if want_to == 9:
        change_settings()
        return
    if want_to == 3:
        updates()
        return
    if want_to == 1:
        choice_manga_path_word = search()
    if want_to == 2:
        choice_manga_path_word = search_on_collect()
    manga_group_path_word = manga_group(choice_manga_path_word)
    manga_chapter_json = manga_chapter(choice_manga_path_word, manga_group_path_word)
    chapter_allocation(manga_chapter_json)


# 自动更新相关
def updates():
    update_want_to = 0
    have_list = load_updates()
    if have_list:
        update_list()
        update_want_to = int(
            Prompt.ask(
                "您是想添加漫画还是删除漫画？[italic yellow](0:添加,1:删除)[/]",
                choices=["0", "1"],
                default="0",
            ),
        )
    if update_want_to == 0:
        new_update = add_updates()
        response = requests.get(
            f"https://api.{config.SETTINGS['api_url']}/api/v3/comic/{new_update[0]}/group/{new_update[1]}"
            f"/chapters?limit=500&offset=0&platform=3",
            headers=config.API_HEADER,
            proxies=config.PROXIES,
        )
        # 记录API访问量
        api_restriction()
        try:
            response.raise_for_status()
        except Exception as e:
            time.sleep(5)
            response.raise_for_status()

        manga_chapter_json = response.json()

        result_total = manga_chapter_json['results']['total']
        raw_manga_list = manga_chapter_json['results']['list']
        manga_list = list(filter(lambda x: x['type'] == 1, raw_manga_list))
        manga_total = len(manga_list)
        is_ok = False
        while not is_ok:
            manga_now = int(
                Prompt.ask(
                    f"当前漫画有 {manga_total} ({result_total}) 话的内容，请问您目前看到多少话了",
                ),
            )

            if manga_now > result_total:
                continue

            find_manga = next(
                (manga for manga in manga_list if manga['index'] == manga_now),
                None,
            )
            if find_manga:
                yOrN = Prompt.ask(
                    f"{manga_now} -> name={find_manga['name']} is ok? (y/N)",
                )
                if yOrN.lower() == 'y':
                    is_ok = True

        save_updates(new_update[0], new_update[1], new_update[2], manga_now, False)
    else:
        del_manga_int = int(Prompt.ask("请输入想要删除的漫画前面的序号"))
        save_updates(
            UPDATE_LIST[del_manga_int - 1]['manga_path_word'],
            UPDATE_LIST[del_manga_int - 1]['manga_group_path_word'],
            UPDATE_LIST[del_manga_int - 1]['manga_name'],
            0,
            True,
        )


def add_updates():
    search_content = Prompt.ask("您需要搜索添加什么漫画呢")
    url = (
        "https://api.%s/api/v3/search/comic?format=json&platform=3&q=%s&limit=10&offset={}"
        % (
            config.SETTINGS["api_url"],
            search_content,
        )
    )
    offset = 0
    current_page_count = 1
    while True:
        # 发送GET请求
        selection = search_list(url, offset, current_page_count)
        data = selection[1]
        selection = selection[0]
        if selection.upper() == "Q":
            break
        try:
            # 将用户输入的字符串转换为整数
            index = int(selection) - 1
            # 获取用户选择的comic的名称并输出
            print("你选择了：{}".format(data["results"]["list"][index]["name"]))
            # 让用户选择分组
            manga_group_path_word = manga_group(
                data["results"]["list"][index]["path_word"],
            )
            # 返回两个pathWord与漫画名称
            return (
                data["results"]["list"][index]["path_word"],
                manga_group_path_word,
                data["results"]["list"][index]["name"],
            )

        except (ValueError, IndexError):
            offset = page_turning(selection, offset, data, current_page_count)
            current_page_count = offset[1]
            offset = offset[0]


def load_updates():
    global UPDATE_LIST
    update_filename = "update.toml"
    # 获取用户目录的路径
    home_dir = os.path.expanduser("~")
    updates_path = os.path.join(home_dir, f".copymanga-downloader/{update_filename}")
    # 检查是否有文件
    if not os.path.exists(updates_path):
        print(f"[yellow]{update_filename}文件不存在,请添加需要更新的漫画[/]")
        return False

    with open(updates_path, 'r') as fp:
        UPDATE_LIST = tomlkit.load(fp)

    if len(UPDATE_LIST['manga']) <= 0:
        print(f"[yellow]{update_filename}文件为空,请添加需要更新的漫画[/]")
        return False

    return True


def update_list():
    for idx, comic_key in enumerate(UPDATE_LIST['manga']):
        comic = UPDATE_LIST['manga'][comic_key]
        print(f"[{idx+1}] {comic['manga_name']}")


def save_updates(
    manga_path_word,
    manga_group_path_word,
    manga_name,
    now_chapter,
    will_del,
):
    global UPDATE_LIST
    update_filename = "update.toml"

    home_dir = os.path.expanduser("~")
    if not os.path.exists(os.path.join(home_dir, '.copymanga-downloader/')):
        os.mkdir(os.path.join(home_dir, '.copymanga-downloader/'))
    updates_path = os.path.join(home_dir, f".copymanga-downloader/{update_filename}")
    # 是否删除漫画
    if will_del:
        for i, item in enumerate(UPDATE_LIST['manga']):
            if item.get('manga_name') == manga_name:
                del UPDATE_LIST[i]
                break
        print(f"[yellow]已将{manga_name}从自动更新列表中删除[/]")
    else:
        # 将新的漫画添加到LIST中
        new_update = {
            "manga_name": manga_name,
            "manga_group_path_word": manga_group_path_word,
            "now_chapter": now_chapter,
        }
        UPDATE_LIST['manga'][manga_path_word] = new_update

        print(
            f"[yellow]已将{manga_name}添加到自动更新列表中,请使用命令行参数‘--subscribe 1’进行自动更新[/]",
        )

    with open(updates_path, 'w') as fp:
        tomlkit.dump(UPDATE_LIST, fp)


# 判断是否已经有了，此函数是为了追踪用户下载到哪一话
def save_new_update(chapter_name, manga_path_word, now_chapter):
    global UPDATE_LIST
    update_filename = "update.toml"

    home_dir = os.path.expanduser("~")
    if not os.path.exists(os.path.join(home_dir, '.copymanga-downloader/')):
        os.mkdir(os.path.join(home_dir, '.copymanga-downloader/'))
    updates_path = os.path.join(home_dir, f".copymanga-downloader/{update_filename}")

    UPDATE_LIST['manga'][manga_path_word]['now_chapter'] = now_chapter
    UPDATE_LIST['manga'][manga_path_word]['now_chapter'].comment(f"{chapter_name}")

    with open(updates_path, 'w') as fp:
        tomlkit.dump(UPDATE_LIST, fp)


def update_download():
    load_settings()
    update_filename = "update.toml"
    if not load_updates():
        console.status(f"[red]{update_filename}并没有内容，请使用正常模式添加！[/]")
        sys.exit()

    for comic_key, comic in UPDATE_LIST['manga'].items():
        console.status(f"[yellow]正在准备🔻{comic['manga_name']}[/]")
        if manga_chapter := update_get_chapter(comic_key, comic):
            chapter_allocation(comic_key, manga_chapter)

        #break # Debug only on one


def update_get_chapter(manga_key, comic):
    manga_name = comic['manga_name']
    manga_group_path_word = comic['manga_group_path_word']
    now_chapter = comic['now_chapter']
    download_max = 50
    # 因为将偏移设置到最后下载的章节，所以可以直接下载全本
    response = requests.get(
        f"https://api.{config.SETTINGS['api_url']}/api/v3/comic/{manga_key}/group/{manga_group_path_word}"
        f"/chapters?limit={download_max}&offset={now_chapter}&platform=3",
        headers=config.API_HEADER,
        proxies=config.PROXIES,
    )
    # 记录API访问量
    api_restriction()

    try:
        response.raise_for_status()
    except Exception as err:
        console.log(err)
        return None

    manga_chapter_json = response.json()
    # Todo 创建传输的json,并且之后会将此json保存为temp.json修复这个问题https://github.com/misaka10843/copymanga-downloader/issues/35
    return_json = {
        "json": manga_chapter_json,
        "start": -1,
        "end": -1,
    }
    # TODO 支持500+话的漫画(感觉并不太需要)
    # console.log(manga_chapter_json)
    # {
    #     'index': 184,
    #     'uuid': 'b443b5ec-f192-11ee-9105-69ffca9e099a',
    #     'count': 192,
    #     'ordered': 1610,
    #     'size': 16,
    #     'name': '第161话',
    #     'comic_id': '259e688c-f526-11e8-b542-00163e0ca5bd',
    #     'comic_path_word': 'dianjuren',
    #     'group_id': None,
    #     'group_path_word': 'default',
    #     'type': 1,
    #     'img_type': 1,
    #     'news': 'success',
    #     'datetime_created': '2024-04-03',
    #     'prev': '698dc3e6-ecae-11ee-8daa-55b00c27fb36',
    #     'next': '89ef1d56-f730-11ee-932f-69ffca9e099a'
    # }
    if not manga_chapter_json['results']['list']:
        print(
            f"[yellow]{manga_name}[/] [bold blue]此漫画并未有新的章节，我们将跳过此漫画[/] idx: {now_chapter}",
        )
        return None

    if manga_chapter_json['results']['total'] > 500:
        print("[bold red]我们暂时不支持下载到500话以上，还请您去Github中创建Issue！[/]")
        return None

    return return_json


# 搜索相关


def search_list(url, offset, current_page_count):
    response = requests.get(
        url.format(offset),
        headers=config.API_HEADER,
        proxies=config.PROXIES,
    )
    # 记录API访问量
    api_restriction()
    # 解析JSON数据
    data = response.json()

    console.rule(f"[bold blue]当前为第{current_page_count}页")
    # 输出每个comic的名称和对应的序号
    for i, comic in enumerate(data["results"]["list"]):
        print("[{}] {}".format(i + 1, comic["name"]))

    # 让用户输入数字来选择comic
    selection = Prompt.ask(
        "请选择一个漫画[italic yellow]（输入Q退出,U为上一页,D为下一页）[/]",
    )
    return selection, data


def page_turning(selection, offset, data, current_page_count):
    # 判断是否是输入的U/D
    # 根据用户输入更新offset
    if selection.upper() == "U":
        offset -= data["results"]["limit"]
        if offset < 0:
            offset = 0
        else:
            current_page_count -= 1
    elif selection.upper() == "D":
        offset += data["results"]["limit"]
        if offset > data["results"]["total"]:
            offset = data["results"]["total"] - data["results"]["limit"]
        else:
            current_page_count += 1
    else:
        # 处理输入错误的情况
        print("[italic red]无效的选择！[/]")
    return offset, current_page_count


def search():
    search_content = Prompt.ask("您需要搜索什么漫画呢")
    url = (
        "https://api.%s/api/v3/search/comic?format=json&platform=3&q=%s&limit=10&offset={}"
        % (
            config.SETTINGS["api_url"],
            search_content,
        )
    )
    offset = 0
    current_page_count = 1
    while True:
        # 发送GET请求
        selection = search_list(url, offset, current_page_count)
        data = selection[1]
        selection = selection[0]
        if selection.upper() == "Q":
            break
        try:
            # 将用户输入的字符串转换为整数
            index = int(selection) - 1
            # 获取用户选择的comic的名称并输出
            print("你选择了：{}".format(data["results"]["list"][index]["name"]))
            # 返回pathWord
            return data["results"]["list"][index]["path_word"]

        except (ValueError, IndexError):
            offset = page_turning(selection, offset, data, current_page_count)
            current_page_count = offset[1]
            offset = offset[0]


# 收藏相关


def search_on_collect():
    url = (
        "https://%s/api/v3/member/collect/comics?limit=12&offset={}&free_type=1&ordering=-datetime_modifier"
        % (config.SETTINGS["api_url"])
    )
    config.API_HEADER['authorization'] = config.SETTINGS['authorization']
    offset = 0
    current_page_count = 1
    retry_count = 0
    while True:
        # 发送GET请求
        response = requests.get(
            url.format(offset),
            headers=config.API_HEADER,
            proxies=config.PROXIES,
        )
        # 记录API访问量
        api_restriction()
        # 解析JSON数据
        data = response.json()
        if data['code'] == 401:
            settings_dir = os.path.join(
                os.path.expanduser("~"),
                ".copymanga-downloader/settings.json",
            )
            if config.SETTINGS["loginPattern"] == "1":
                print(f"[bold red]请求出现问题！疑似Token问题！[{data['message']}][/]")
                print(
                    f"[bold red]请删除{settings_dir}来重新设置！(或者也可以自行修改配置文件)[/]",
                )
                sys.exit()
            else:
                res = login(
                    **login_information_builder(
                        config.SETTINGS["username"],
                        config.SETTINGS["password"],
                        config.SETTINGS["api_url"],
                        config.SETTINGS["salt"],
                        config.PROXIES,
                    ),
                )
                if res:
                    config.API_HEADER['authorization'] = f"Token {res}"
                    config.SETTINGS["authorization"] = f"Token {res}"
                    save_settings(config.SETTINGS)
                    continue
                time.sleep(2**retry_count)  # 重试时间指数
                retry_count += 1

        console.rule(f"[bold blue]当前为第{current_page_count}页")
        # 输出每个comic的名称和对应的序号
        for i, comic in enumerate(data["results"]["list"]):
            print("[{}] {}".format(i + 1, comic['comic']["name"]))

        # 让用户输入数字来选择comic
        selection = Prompt.ask(
            "请选择一个漫画[italic yellow]（输入Q退出,U为上一页,D为下一页）[/]",
        )
        if selection.upper() == "Q":
            break
        try:
            # 将用户输入的字符串转换为整数
            index = int(selection) - 1
            # 获取用户选择的comic的名称并输出
            print(
                "你选择了：{}".format(data["results"]["list"][index]['comic']["name"]),
            )
            # 返回pathWord
            return data["results"]["list"][index]['comic']["path_word"]

        except (ValueError, IndexError):
            offset = page_turning(selection, offset, data, current_page_count)
            current_page_count = offset[1]
            offset = offset[0]


def collect_expect():
    url = f"https://api.{config.SETTINGS['api_url']}/api/v3/member/collect/comics"
    params = {
        "limit": 12,
        "offset": 0,
    }
    data = []
    want_to = int(
        Prompt.ask(
            f"请问是输出json格式还是csv格式？" f"[italic yellow](0:json,1:csv)[/]",
            choices=["0", "1"],
            default="1",
        ),
    )
    while True:
        config.API_HEADER['authorization'] = config.SETTINGS['authorization']
        res = requests.get(url, params=params, headers=config.API_HEADER)
        res_json = json.loads(res.text)
        if res_json["code"] != 200:
            print(
                f"[bold red]无法获取到相关信息，请检查相关设置。Error:{res_json['message']}",
            )
            return
        for item in res_json['results']['list']:
            comic = item['comic']
            data.append(
                [
                    comic['name'],
                    comic['path_word'],
                    comic['datetime_updated'],
                    comic['last_chapter_name'],
                ],
            )

        if len(data) >= res_json['results']['total']:
            break
        else:
            params['offset'] += 12
    if want_to == 0:
        # 输出到test.json
        with open('collect.json', 'w') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print("[green]已将您的收藏输出到运行目录下的collect.json中[/]")
    else:
        with open('collect.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Name', 'Path Word', 'Update Time', 'Last Chapter'])
            writer.writerows(data)
        print("[green]已将您的收藏输出到运行目录下的collect.csv中[/]")


# 漫画详细相关


def manga_group(manga_path_word):
    response = requests.get(
        f"https://api.{config.SETTINGS['api_url']}/api/v3/comic2/{manga_path_word}",
        headers=config.API_HEADER,
        proxies=config.PROXIES,
    )
    # 记录API访问量
    api_restriction()
    try:
        response.raise_for_status()
    except Exception as e:
        time.sleep(5)
        response.raise_for_status()
    manga_group_json = response.json()
    # 判断是否只有默认组
    if len(manga_group_json["results"]["groups"]) == 1:
        return "default"

    manga_group_path_word_list = []
    # 获取group值并强转list
    for i, manga_group_list in enumerate(manga_group_json["results"]["groups"]):
        print(
            f"{i + 1}->{manga_group_json['results']['groups'][manga_group_list]['name']}",
        )
        # 将分组的path_word添加到数组中
        manga_group_path_word_list.append(
            manga_group_json['results']['groups'][manga_group_list]['path_word'],
        )
    choice = IntPrompt.ask("请输入要下载的分组前面的数字")
    return manga_group_path_word_list[choice - 1]


def manga_chapter(manga_path_word, group_path_word):
    response = requests.get(
        f"https://api.{config.SETTINGS['api_url']}/api/v3/comic/{manga_path_word}/group/{group_path_word}"
        f"/chapters?limit=500&offset=0&platform=3",
        headers=config.API_HEADER,
        proxies=config.PROXIES,
    )
    # 记录API访问量
    api_restriction()
    try:
        response.raise_for_status()
    except Exception as e:
        time.sleep(5)
        response.raise_for_status()

    manga_chapter_json = response.json()
    # Todo 创建传输的json,并且之后会将此json保存为temp.json修复这个问题https://github.com/misaka10843/copymanga-downloader/issues/35
    return_json = {
        "json": manga_chapter_json,
        "start": None,
        "end": None,
    }
    # Todo 支持500+话的漫画(感觉并不太需要)
    if manga_chapter_json['results']['total'] > 500:
        print("[bold red]我们暂时不支持下载到500话以上，还请您去Github中创建Issue！[/]")
        sys.exit()
    # 询问应该如何下载
    # 如果是命令行参数就直接返回对应
    if ARGS:
        return_json["start"] = int(ARGS.MangaStart) - 1
        return_json["end"] = int(ARGS.MangaEnd)
        return return_json
    want_to = int(
        Prompt.ask(
            f"获取到{manga_chapter_json['results']['total']}话内容，请问如何下载?"
            f"[italic yellow](0:全本下载,1:范围下载,2:单话下载)[/]",
            choices=["0", "1", "2"],
            default="0",
        ),
    )
    if want_to == 0:
        return_json["start"] = -1
        return_json["end"] = -1
        return return_json
    print(
        "[italic yellow]请注意！此话数包含了其他比如特别篇的话数，比如”第一话，特别篇，第二话“，那么第二话就是3，而不2[/]",
    )
    if want_to == 1:
        return_json["start"] = int(Prompt.ask("请输入开始下载的话数")) - 1
        print(
            f"[italic blue]您选择从[yellow]{manga_chapter_json['results']['list'][return_json['start']]['name']}"
            f"[/yellow]开始🔻[/]",
        )
        return_json["end"] = int(Prompt.ask("请输入结束下载的话数"))
        print(
            f"[italic blue]您选择在[yellow]{manga_chapter_json['results']['list'][return_json['end']]['name']}"
            f"[/yellow]结束🔻[/]",
        )
        return return_json
    if want_to == 2:
        return_json["start"] = int(Prompt.ask("请输入需要下载的话数")) - 1
        return_json["end"] = return_json["start"]
        print(
            f"[italic blue]您选择下载[yellow]{manga_chapter_json['results']['list'][return_json['end']]['name']}[/]",
        )
        return return_json


def chapter_allocation(manga_key, manga_chapter_json):
    if manga_chapter_json['start'] < 0:
        manga_chapter_list = manga_chapter_json['json']['results']['list']
    elif manga_chapter_json['start'] == manga_chapter_json['end']:
        # 转换为一个只包含一个元素的数组
        manga_chapter_list = [
            manga_chapter_json['json']['results']['list'][manga_chapter_json['start']],
        ]
    else:
        manga_chapter_list = manga_chapter_json['json']['results']['list'][
            manga_chapter_json['start'] : manga_chapter_json['end']
        ]
    # 准备分配章节下载
    for manga_chapter_info in manga_chapter_list:
        response = requests.get(
            f"https://api.{config.SETTINGS['api_url']}/api/v3/comic/{manga_chapter_info['comic_path_word']}"
            f"/chapter2/{manga_chapter_info['uuid']}?platform=3",
            headers=config.API_HEADER,
            proxies=config.PROXIES,
        )

        # 记录API访问量
        api_restriction()

        try:
            response.raise_for_status()
        except Exception:
            time.sleep(5)
            response.raise_for_status()

        manga_chapter_info_json = response.json()

        img_url_contents = manga_chapter_info_json['results']['chapter']['contents']
        img_words = manga_chapter_info_json['results']['chapter']['words']
        raw_manga_name = manga_chapter_info_json['results']['comic']['name']
        special_chars = string.punctuation + ' '
        manga_name = ''.join(c for c in raw_manga_name if c not in special_chars)
        num_images = len(img_url_contents)
        download_path = config.SETTINGS['download_path']
        chapter_name = manga_chapter_info_json['results']['chapter']['name']
        # 检查漫画文件夹是否存在

        if not os.path.exists(f"{download_path}/{manga_name}/"):
            os.mkdir(f"{download_path}/{manga_name}/")

        chapter_path = f"{download_path}/{manga_name}/{chapter_name}/"
        if not os.path.exists(chapter_path):
            os.mkdir(chapter_path)

        def _create_cbz():
            if config.SETTINGS['CBZ']:
                with console.status(
                    f"[bold yellow]正在保存CBZ存档:[{manga_name}]{chapter_name}[/]",
                ):
                    # comic_path = manga_chapter_info_json['results']['chapter']['comic_path_word']
                    create_cbz(
                        str(
                            int(manga_chapter_info_json['results']['chapter']['index'])
                            + 1,
                        ),
                        chapter_name,
                        manga_name,
                        f"{manga_name}/{chapter_name}/",
                        config.SETTINGS['cbz_path'],
                        manga_name,
                    )
                console.status(
                    f"[bold green][:white_check_mark:]已将[{manga_name}]{chapter_name}保存为CBZ存档[/]",
                )

        download_queue = queue.Queue()

        def thread_worker(the_queue, num_images, track_desc):
            for i in track(
                range(num_images),
                total=num_images,
                description=track_desc,
            ):
                url, filename = the_queue.get()
                if file_download := download(url, filename):
                    if not file_download:
                        time.sleep(0.5)  # 添加一点延迟，错峰请求
                the_queue.task_done()

            # Only need to work at latest one
            _create_cbz()

        idx_id = int(manga_chapter_info_json['results']['chapter']['index']) + 1
        track_desc = f"[yellow]🔻[{manga_name}]{chapter_name}(idx: {idx_id})[/]"

        for i in range(num_images):
            url = img_url_contents[i]['url']
            file_name = os.path.join(
                chapter_path,
                f"{str(img_words[i] + 1).zfill(3)}.jpg",
            )
            download_queue.put((url, file_name))

        t = threading.Thread(
            target=thread_worker,
            args=(download_queue, num_images, track_desc),
        )
        t.start()
        t.join()

        # 实施添加下载进度
        if ARGS and ARGS.subscribe == "1":
            save_new_update(
                chapter_name,
                manga_chapter_info_json['results']['chapter']['comic_path_word'],
                manga_chapter_info_json['results']['chapter']['index'] + 1,
            )

        console.status(
            f"[bold green][:white_check_mark:][{manga_name}]{chapter_name}🔻🆗[/]",
        )

        # epub_transformerhelper(download_path, manga_name, chapter_name)
        _create_cbz()


# 下载相关


def download(url, filename, overwrite=False):
    # 判断是否已经下载
    if not overwrite and os.path.exists(filename):
        # print(f"[blue]您已经下载了{filename}，跳过下载[/]")
        return True

    img_api_restriction()

    if config.SETTINGS['HC'] == "1":
        url = url.replace("c800x.jpg", "c1500x.jpg")

    response = None
    try:
        response = requests.get(url, headers=config.API_HEADER, proxies=config.PROXIES)
    except Exception:
        # 重新尝试一次
        try:
            time.sleep(3)
            response = requests.get(
                url,
                headers=config.API_HEADER,
                proxies=config.PROXIES,
            )
        except Exception as e:
            print(
                f"[bold red]无法🔻{filename}，似乎是CopyManga暂时屏蔽了您的IP，请稍后手动下载对应章节(章节话数为每话下载输出的索引ID),ErrMsg:{e}[/]",
            )
            return False
    finally:
        if response:
            with open(filename, "wb") as f:
                f.write(response.content)
            return True

    return False


def main():
    global ARGS
    loaded_settings = load_settings()
    if not loaded_settings[0]:
        print(f"[bold red]{loaded_settings[1]},我们将重新为您设置[/]")
        set_settings()
    parse_args()
    if ARGS:
        if ARGS.subscribe == "1":
            print(
                "[bold purple]请注意！此模式下可能会导致部分img下载失败，如果遇见报错还请您自行删除更新列表然后重新添加后运行，此程序会重新下载并跳过已下载内容[/]",
            )
            update_download()
            sys.exit()
        if ARGS.MangaPath and ARGS.MangaEnd and ARGS.MangaStart:
            command_mode()
            # 防止运行完成后又触发正常模式
            sys.exit()
        else:
            print("[bold red]命令行参数中缺少必要字段,将切换到普通模式[/]")
            ARGS = None
    welcome()


if __name__ == '__main__':
    try:
        main()
    # Ctrl+C
    except KeyboardInterrupt:
        print('Received keyboard interrupt')
        sys.exit()
    except SystemExit:
        sys.exit()
