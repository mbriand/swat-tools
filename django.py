#!/usr/bin/env python3

import requests
import pickle
import pprint
import pathlib
import click

BINDIR = pathlib.Path(__file__).parent.resolve()
DATADIR = BINDIR / "data"
COOKIESFILE = DATADIR / 'cookies'

LOGIN_URL = "https://swatbot.yoctoproject.org/accounts/login/"

SESSION = requests.Session()

@click.group()
def main():
    pass

@main.command()
@click.argument('user')
@click.argument('password')
def login(user, password):
    r = SESSION.get(LOGIN_URL)
    r.raise_for_status()

    data = {
        "csrfmiddlewaretoken": SESSION.cookies['csrftoken'],
        "username": user,
        "password": password
    }
    r = SESSION.post(LOGIN_URL, data=data)

    if r.status_code not in [requests.codes.ok, requests.codes.not_found]:
        r.raise_for_status()

    COOKIESFILE.parent.mkdir(parents=True, exist_ok=True)
    with COOKIESFILE.open('wb') as f:
        pickle.dump(SESSION.cookies, f)

@main.command()
def testreq():
    with COOKIESFILE.open('rb') as f:
        SESSION.cookies.update(pickle.load(f))

    r = SESSION.get('https://swatbot.yoctoproject.org/rest/build/380450/')
    print(r)
    pprint.pprint(r.text)


if __name__ == '__main__':
    main()
