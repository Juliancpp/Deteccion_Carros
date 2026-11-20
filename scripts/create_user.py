"""Create an SQLite user with a hidden, interactive password."""
import argparse
from getpass import getpass
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import administration, config
from app.hotel.store import HotelStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--username')
    parser.add_argument('--role', choices=['admin', 'operator'])
    args = parser.parse_args()
    username = args.username or input('Username: ')
    role = args.role or input('Role [admin/operator]: ').strip()
    password = getpass('Password (mínimo 12 caracteres): ')
    if password != getpass('Confirm password: '):
        parser.exit(1, 'Las contraseñas no coinciden.\n')
    try:
        store = HotelStore(config.HOTEL_DATABASE_PATH)
        store.close()
        administration.migrate()
        administration.create_user(username, password, role)
    except (ValueError, sqlite3.IntegrityError) as exc:
        parser.exit(1, f'No se creó el usuario: {exc}\n')
    print(f'Usuario {username.strip()} creado ({role}) en {Path(config.HOTEL_DATABASE_PATH).resolve()}.')


if __name__ == '__main__':
    main()
