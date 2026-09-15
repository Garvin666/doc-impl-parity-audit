import argparse

p = argparse.ArgumentParser()
p.add_argument("--real-flag")
p.add_argument("--impl-only-flag")
sub = p.add_subparsers()
sub.add_parser("run")
sub.add_parser("hidden-subcmd")

VALID_STATUS = ("完成", "进行中")
