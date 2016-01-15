import argparse
import hashlib
import linecache
import os
import shlex
import sqlite3
import stat
import sys

import six
from binaryornot.check import is_binary

if six.PY2:
    def blob(x):
        return sqlite3.Binary(x)

_db_cache = None

_shlex_settings = {
    '.default': {
    },
    '.override0': {
    },
    '.override1': {
        'whitespace_split': True
    },
    '.override2': {
        'quotes': ""
    },
}

_ignore_dir = set([
    ".git",
    ".svn"
])


class TokenDict(dict):
    def __init__(self, db, *args, **kwargs):
        super(TokenDict, self).__init__(*args, **kwargs)
        self.db = db

    def __missing__(self, key):
        with self.db:
            cur = self.db.cursor()
            res = cur.execute("""
                SELECT
                    id
                FROM
                    token
                WHERE
                    string = ?;
            """, (blob(key),)).fetchall()
            if res:
                ret = res[0][0]
            else:
                cur.execute("""
                    INSERT INTO
                        token(string)
                    VALUES
                        (?);
                """, (blob(key),))
                ret = cur.lastrowid
        self[key] = ret
        return ret


def cleanup(string):
    if len(string) <= 16:
        return string.lower()
    return hashlib.md5(string.lower()).digest()


def get_db(create=False):
    global _db_cache
    if _db_cache:
        return _db_cache  # noqa
    exists = os.path.exists("FINJA")
    if not (create or exists):
        raise ValueError("Could not find FINJA")
    connection = sqlite3.connect("FINJA")  # noqa
    if not exists:
        connection.execute("""
            CREATE TABLE
                finja(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    file_id INTEGER,
                    line INTEGER
                );
        """)
        connection.execute("""
            CREATE INDEX finja_token_id_idx ON finja (token_id);
        """)
        connection.execute("""
            CREATE INDEX finja_file_idx ON finja (file_id);
        """)
        connection.execute("""
            CREATE TABLE
                token(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    string BLOB
                );
        """)
        connection.execute("""
            CREATE INDEX token_string_idx ON token (string);
        """)
        connection.execute("""
            CREATE TABLE
                file(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT,
                    inode INTEGER,
                    found INTEGER DEFAULT 1
                );
        """)
        connection.execute("""
            CREATE INDEX file_path_idx ON file (path);
        """)
    _db_cache = (connection, TokenDict(connection))
    return _db_cache


def apply_shlex_settings(pass_, ext, lex):
    to_apply = [_shlex_settings['.default']]
    if ext in _shlex_settings:
        to_apply.append(_shlex_settings[ext])
    to_apply.append(
        _shlex_settings['.override%s' % pass_]
    )
    for settings in to_apply:
        for key in settings.keys():
            setattr(lex, key, settings[key])


def index_file(db, file_path, update = False):
    con        = db[0]
    token_dict = db[1]
    mode       = os.stat(file_path)
    inode      = mode[stat.ST_INO]
    old_inode  = None
    file_      = None
    with con:
        res = con.execute("""
            SELECT
                id,
                inode
            FROM
                file
            WHERE
                path=?;
        """, (file_path,)).fetchall()
        if res:
            file_     = res[0][0]
            old_inode = res[0][1]
    if old_inode != inode:
        with con:
            if file_ is None:
                cur = con.cursor()
                cur.execute("""
                    INSERT INTO
                        file(path, inode)
                    VALUES
                        (?, ?);
                """, (file_path, inode))
                file_ = cur.lastrowid
        inserts = []
        if is_binary(file_path):
            if not update:
                print("%s: is binary, skipping" % (file_path,))
        else:
            pass_ = 0
            with open(file_path, "r") as f:
                while pass_ <= 2:
                    try:
                        f.seek(0)
                        lex = shlex.shlex(f, file_path)
                        ext = file_path.split(os.path.extsep)[-1]
                        apply_shlex_settings(
                            pass_,
                            ext,
                            lex
                        )
                        t = lex.get_token()
                        while t:
                            word = cleanup(t)
                            inserts.append((
                                token_dict[word],
                                file_,
                                lex.lineno
                            ))
                            t = lex.get_token()
                    except ValueError:
                        if pass_ >= 2:
                            raise
                    pass_ += 1
            all_inserts    = len(inserts)
            inserts        = list(set(inserts))
            unique_inserts = len(inserts)
            print("%s: indexed %s/%s" % (
                file_path,
                all_inserts,
                unique_inserts
            ))
        with con:
            con.execute("""
                DELETE FROM
                    finja
                WHERE
                    file_id=?;
            """, (file_,))
            con.executemany("""
                INSERT INTO
                    finja(token_id, file_id, line)
                VALUES
                    (?, ?, ?);
            """, inserts)
    else:
        if not update:
            print("%s: uptodate" % (file_path,))
        with con:
            con.execute("""
                UPDATE
                    file
                SET
                    inode = ?,
                    found = 1
                WHERE
                    id = ?
            """, (inode, file_))


def index():
    db = get_db(create=True)
    do_index(db)


def do_index(db, update=False):
    con = db[0]
    with con:
        con.execute("""
            UPDATE
                file
            SET found = 0
        """)
    for dirpath, _, filenames in os.walk("."):
        if set(dirpath.split(os.sep)).intersection(_ignore_dir):
            continue
        for filename in filenames:
            file_path = os.path.join(
                dirpath,
                filename
            )
            index_file(db, file_path, update)
    with con:
        con.execute("""
            DELETE FROM
                finja
            WHERE
                file_id IN (
                    SELECT
                        id
                    FROM
                        file
                    WHERE
                        found = 0
                )
        """)
        con.execute("""
            DELETE FROM
                file
            WHERE
                found = 0
            """)
        con.execute("VACUUM;")


def find_finja():
    cwd = os.path.abspath(".")
    lcwd = cwd.split(os.sep)
    while lcwd:
        cwd = os.sep.join(lcwd)
        check = os.path.join(cwd, "FINJA")
        if os.path.isfile(check):
            return cwd
        lcwd.pop()
    raise ValueError("Could not find FINJA")


def search(
        search,
        file_mode=False,
        update=False,
):
    finja = find_finja()
    os.chdir(finja)
    db = get_db(create=False)
    con = db[0]
    token_dict = db[1]
    if update:
        do_index(db, update=True)
    res = []
    with con:
        if file_mode:
            for word in search:
                word = cleanup(word)
                token = token_dict[word]
                res.append(set(con.execute("""
                    SELECT DISTINCT
                        file
                    FROM
                        finja
                    WHERE
                        token_id=?
                """, (token,)).fetchall()))
        else:
            for word in search:
                word = cleanup(word)
                token = token_dict[word]
                res.append(set(con.execute("""
                    SELECT DISTINCT
                        f.path,
                        i.line
                    FROM
                        finja as i
                    JOIN
                        file as f
                    ON
                       i.file_id = f.id
                    WHERE
                        token_id=?
                """, (token,)).fetchall()))
    res_set = res.pop()
    for search_set in res:
        res_set.intersection_update(search_set)
    if file_mode:
        for match in res_set:
            print(match[0])
    else:
        for match in res_set:
            path = match[0]
            if path.startswith("./"):
                path = path[2:]
            print("%s:%5d\t%s" % (
                path,
                match[1],
                linecache.getline(match[0], match[1])[:-1]
            ))


def main(argv=None):
    """Parse the args and excute"""
    if not argv:  # pragma: no cover
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(description='Index and find stuff')
    parser.add_argument(
        '--index',
        '-i',
        help='Index the current directory',
        action='store_true',
    )
    parser.add_argument(
        '--update',
        '-u',
        help='Update the index before searching',
        action='store_true',
    )
    parser.add_argument(
        '--file-mode',
        '-f',
        help='Ignore line-number when matching search strings',
        action='store_true',
    )
    parser.add_argument(
        'search',
        help='search string',
        type=str,
        nargs='*',
        default='all'
    )
    args = parser.parse_args(argv)
    if args.index:
        index()
    if args.search:
        search(
            args.search,
            file_mode=args.file_mode,
            update=args.update
        )
