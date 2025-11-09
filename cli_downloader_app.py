import os
import time
import requests
import threading
from math import ceil
from concurrent.futures import ThreadPoolExecutor, as_completed

import sys
import json
import argparse
import subprocess
from pathlib import Path
from pprint import pprint
from _standbylock import StandbyLock

from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TextColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
)


SCRIPT_DIR = Path(__file__).resolve().parent
TXT_DATA_FILEPATH = str(SCRIPT_DIR.joinpath("url_inputs.txt"))
JSON_DATA_FILEPATH = str(SCRIPT_DIR.joinpath("url_inputs.json"))
UNFINISHED_DOWNLOADS_FILEPATH = str(SCRIPT_DIR.joinpath("unfinished_downloads.json"))


def choose_threads(file_size):
    """Decide num_threads_per_file dynamically based on file size."""
    if file_size < 50 * 1024 * 1024:   # < 50MB
        return 1
    elif file_size < 500 * 1024 * 1024:  # 50MB–500MB
        return min(4, max(2, file_size // (50 * 1024 * 1024)))
    else:  # > 500MB
        return min(16, max(4, file_size // (100 * 1024 * 1024)))


# -------------------------------
# Worker function (common)
# -------------------------------
def download_range(url, filename, start, end, part_num, progress_lock, progress_state, update_fn):
    """Download a single range of a file and report progress via update_fn."""
    part_file = f"{filename}.part{part_num}"
    downloaded = os.path.getsize(part_file) if os.path.exists(part_file) else 0

    while downloaded < (end - start + 1):
        headers = {
            "Range": f"bytes={start + downloaded}-{end}",
            "User-Agent": "Mozilla/5.0",  # Pretend to be a browser
        }
        try:
            with requests.get(url, headers=headers, stream=True, timeout=10) as r:
                r.raise_for_status()
                with open(part_file, "ab") as f:
                    for chunk in r.iter_content(8192):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        with progress_lock:
                            progress_state[0] += len(chunk)
                            update_fn(progress_state[0])
            break
        except Exception as e:
            wait_time = min(60, 2 ** min(downloaded // 1_000_000, 6))  # up to 64s
            print(f"[{Path(filename).name} - Part {part_num}] Error: {e} -> retrying in {wait_time}s")
            time.sleep(wait_time)


# -------------------------------
# Rich implementation
# -------------------------------
def download_file_rich(url, filename, executor, progress):
    response = requests.head(url, allow_redirects=True)

    content_type = response.headers.get("Content-Type", "").lower()
    if (response.status_code < 200 or response.status_code > 299):
        print(response.status_code)
        print(response.headers['Location'], '\n')
    if "Content-Length" not in response.headers:
        print(f'Error :: \n\t{url}\n\tNo Content-Length\n\tContent-Type = {content_type}')
        print("Redirect Location:", response.headers.get("Location"))
        raise Exception("Server does not provide Content-Length (can't download).")

    if "text/html" in content_type or "application/json" in content_type:
        raise Exception(f"URL points to non-downloadable content (Content-Type: {content_type})")

    file_size = int(response.headers["Content-Length"])

    if os.path.exists(filename) and os.path.getsize(filename) == file_size:
        print(f"{Path(filename).name} already downloaded.")
        return

    num_threads = choose_threads(file_size)
    part_size = ceil(file_size / num_threads)
    ranges = [(i * part_size, min((i + 1) * part_size - 1, file_size - 1)) for i in range(num_threads)]

    # preload progress
    already_downloaded = sum(
        os.path.getsize(f"{filename}.part{i}") for i in range(num_threads) if os.path.exists(f"{filename}.part{i}")
    )

    task_id = progress.add_task(filename, total=file_size, completed=already_downloaded)

    progress_lock = threading.Lock()
    progress_state = [already_downloaded]

    def update_fn(new_value):
        progress.update(task_id, completed=new_value)

    futures = [
        executor.submit(download_range, url, filename, start, end, i, progress_lock, progress_state, update_fn)
        for i, (start, end) in enumerate(ranges)
    ]

    for f in as_completed(futures):
        f.result()

    progress.update(task_id, completed=file_size)

    # merge parts
    with open(filename, "wb") as outfile:
        for i in range(num_threads):
            part_file = f"{filename}.part{i}"
            with open(part_file, "rb") as infile:
                outfile.write(infile.read())
            os.remove(part_file)

    print(f"✅ Download complete: {filename}")


# -------------------------------
# Entry point
# -------------------------------
def start_download(files, max_workers=None):
    if max_workers is None:
        max_workers = min(32, os.cpu_count() * 2)

    progress_columns = [
        TextColumn("[bold blue]{task.description}", justify="right"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ]

    from rich.console import Console
    console = Console()

    with Progress(*progress_columns, console=console) as progress:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(download_file_rich, url, filename, executor, progress)
                for url, filename in files
            ]
            for f in as_completed(futures):
                f.result()

# Example use
# if __name__ == "__main__":
#     files_to_download = [
#         ("https://example.com/file1.zip", "file1.zip"),
#         ("https://example.com/file2.mp4", "file2.mp4"),
#     ]

#     start_download(files_to_download, max_workers=8)


def get_filepath(url, dir_path, ep=None):
    if ep:
        response = requests.head(url, allow_redirects=True)
        filename = response.headers['Content-Disposition'].split('filename=')[-1]
    else:
        filename = url.split('/')[-1]
    filename = filename.replace('%20', ' ')
    filename = filename.replace('%5B', '[')
    filename = filename.replace('%5D', ']')
    filename = filename.replace('"', '')
    if len(filename) > 205:
        filename = filename[:200] + filename[-4:]
    filepath = Path(dir_path).joinpath(filename)
    return str(filepath)


def parseInputFile(input_filepath, file_type, storage_dir=None):
    """ Returns a list of (url, filepath) tuples """
    if not Path(input_filepath).exists():
        print(f'Error :: Filepath "{input_filepath}" does not exists')
        return
    payload = []
    match file_type:
        case "txt":
            if not storage_dir:
                print(f'Error :: storage filepath required')
                return
            if not Path(storage_dir).exists():
                print(f'Error :: storage filepath "{storage_dir}" does not exists')
                return
            with open(input_filepath, 'r', encoding='utf-8') as fh:
                file_lines = fh.readlines()
            dl_url_lines = [
                line for line in file_lines if not line.strip().startswith("#")
            ]
            for url_line in dl_url_lines:
                url_line = url_line.strip()
                if not url_line or url_line[0] == '#':
                    continue
                if url_line:
                    if url_line[:4] == "ep__":
                        url_line = url_line[4:]
                        filepath = get_filepath(url_line, storage_dir, True)
                    else:
                        filepath = get_filepath(url_line, storage_dir)
                    payload.append((url_line, filepath))
        case "json":
            # filter comments
            with open(input_filepath, 'r', encoding='utf-8') as fh:
                file_lines = fh.readlines()
            dl_url_lines = [
                line for line in file_lines
                if not line.strip().startswith("#") and not line.strip().startswith("//")
            ]
            json_string = "".join(dl_url_lines)
            file_dict = json.loads(json_string)

            for storage_dir in file_dict.keys():
                if not Path(storage_dir).exists():
                    print(f'Error :: Storage path "{input_filepath}" does not exists')
                    continue
                for url in file_dict[storage_dir]:
                    url = url.strip()
                    if not url or url[0] == '#' or url[:2] == "//":
                        continue
                    if url:
                        if url[:4] == "ep__":
                            url = url[4:]
                            filepath = get_filepath(url, storage_dir, True)
                        else:
                            filepath = get_filepath(url, storage_dir)
                        payload.append((url, filepath))
    return payload


def openInputFile(file_type, independent=False):
    """ 
        Opens a notepad instance for editing url input file.
        @independent indicates blocking or non-blocking.
    """
    command = None
    match file_type:
        case "txt":
            command = ["notepad.exe", TXT_DATA_FILEPATH]
        case "json":
            command = ["notepad.exe", JSON_DATA_FILEPATH]
    if command:
        if independent:
            subprocess.Popen(command)
        else:
            subprocess.run(command)


def startInteractiveSession(args):
    ep = None
    url = None
    prompt = None
    filepath = None
    storage_dirpath = None
    files_to_download = list()

    while (prompt != "quit"):
        prompt = input("[Prompt] :: ")
        match prompt.lower():
            case "--save_to":
                prompt = input("[save_to] :: ")
                storage_dirpath = prompt.strip()
                if not (Path(storage_dirpath).exists() and Path(storage_dirpath).is_dir()):
                    print(f'Storage path {storage_dirpath} does not exists or is not a directory.')
                else:
                    storage_dirpath = storage_dirpath
                    print(f'Storage path set to "{storage_dirpath}"')
            case "--url":
                prompt = input("[url] :: ")
                url = prompt.strip()
                if url[:4] == "ep__" or ep:
                    url = url[4:] if url[:4] == "ep__" else url
                    filepath = get_filepath(url, storage_dirpath, True)
                else:
                    filepath = get_filepath(url, storage_dirpath)
                print(f'URL set to "{url}"')
            case "--ep":
                ep = True
                print(f'ep set to "{ep}"')
            case "--txt":
                openInputFile("txt")
            case "--json":
                openInputFile("json")
            case "--dl_url":
                if not storage_dirpath:
                    print("Error :: No storage dir path")
                    continue
                with StandbyLock():
                    start_download([(url, filepath)])
                    print("Download complete\n")
            case "--dl_txt":
                if not storage_dirpath:
                    print("Error :: No storage dir path")
                    continue
                files_to_download = parseInputFile(TXT_DATA_FILEPATH, 'txt', storage_dirpath)
                with StandbyLock():
                    for download_data in files_to_download:
                        start_download([download_data])
            case "--dl_json":
                files_to_download = parseInputFile(JSON_DATA_FILEPATH, 'json')
                with StandbyLock():
                    for download_data in files_to_download:
                        start_download([download_data])
            case "--cls":
                os.system("cls")


# wip
def updateTrackedDownloads(storage_filepath, url, update_type):
    """
        Definitions
        -----------
        * storage_filepath :: the location on a local filesystem to store the downloaded file
        * url :: a valid url to the desired resource
        * update_tye :: whether to track or untrack the given url

        Expected Data
        -------------
        * storage_filepath :: valid file path
        * url :: valid url string
        * update_tye :: string ["add", "remove"]
    """
    ...


def checkInputFiles():
    file_paths = [
        Path(TXT_DATA_FILEPATH), 
        Path(JSON_DATA_FILEPATH), 
        Path(UNFINISHED_DOWNLOADS_FILEPATH)
    ]

    for path in file_paths:
        if not path.exists():
            path.touch()


"""
    Futures
    =======
    [] track unfinished downloads
"""


if __name__ == '__main__':
    checkInputFiles()

    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--save_to")
    parser.add_argument("--ep", action="store_true")
    parser.add_argument("--txt", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--dl_txt", action="store_true")
    parser.add_argument("--dl_json", action="store_true")
    args = parser.parse_args()

    # Interactive mode
    if args.init:
        startInteractiveSession(args)
        sys.exit()

    # Open input file(s) for editing
    if args.txt:
        openInputFile("txt")
        sys.exit()
    if args.json:
        openInputFile("json")
        sys.exit()

    # Non-interactive mode
    if not ((args.save_to and (args.url or args.txt)) or args.dl_json):
        print(f'Error :: Missing startup input(s)')
    else:
        with StandbyLock():
            download_urls = []
            files_to_download = []
            if args.url:
                download_urls.append(args.url)
                filepath = get_filepath(args.url, args.save_to, args.ep)
                start_download([(args.url, filepath)])
            if args.dl_txt:
                files_to_download = parseInputFile(TXT_DATA_FILEPATH, 'txt', args.save_to)
                for download_data in files_to_download:
                    start_download([download_data])
            if args.dl_json:
                files_to_download = parseInputFile(JSON_DATA_FILEPATH, 'json')
                for download_data in files_to_download:
                    start_download([download_data])
