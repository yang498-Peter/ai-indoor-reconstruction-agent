"""Offline administrator bootstrap; the password is read only from stdin."""
import argparse
import sys
from .store import Store


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',required=True)
    parser.add_argument('--username',required=True)
    args=parser.parse_args()
    secret=sys.stdin.readline().rstrip('\r\n')
    if not secret: raise SystemExit('Missing initial password on stdin')
    store=Store(args.data)
    with store.db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            raise SystemExit('An administrator already exists; bootstrap was not repeated')
    store.create_user(args.username,secret,role='admin',must_change=True)
    print('Administrator initialized; first login requires a password change.')


if __name__=='__main__':main()
