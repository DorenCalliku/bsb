from . import BaseCommand


class BsbCompile(BaseCommand, name="compile"):
    def handler(self, namespace):
        pass


class BsbSimulate(BaseCommand, name="simulate"):
    def handler(self, namespace):
        pass


def compile():
    return BsbCompile


def simulate():
    return BsbSimulate
