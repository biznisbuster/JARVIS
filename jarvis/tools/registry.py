"""Explicit canonical registry for public JARVIS tools."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from .apple.calendar import calendar_today
from .apple.reminders import reminders_create, reminders_list
from .base import ToolSpec, _bool_prop, _int_prop, _schema, _str_prop
from .coding.kilo import kilo_run_tool
from .media import (
    ytm_next,
    ytm_pause,
    ytm_play,
    ytm_previous,
    ytm_resume,
    ytm_status,
    ytm_volume_down,
    ytm_volume_mute,
    ytm_volume_set,
    ytm_volume_up,
)
from .search.web import web_search
from .system.apps import open_app, open_url
from .system.clipboard import read_clipboard, write_clipboard
from .system.time import time_now
from .system.volume import system_volume
from .system.youtube import play_youtube


class ToolRegistry:
    """Name-indexed tool registry with schema/name invariants."""

    def __init__(self, definitions: Iterable[ToolSpec] | None = None) -> None:
        self._definitions: dict[str, ToolSpec] = {}
        for definition in definitions or ():
            self.register(definition)

    def register(self, definition: ToolSpec) -> ToolSpec:
        if definition.name in self._definitions:
            raise ValueError(f"duplicate tool registration: {definition.name}")
        function = definition.schema.get("function") if isinstance(definition.schema, dict) else None
        schema_name = function.get("name") if isinstance(function, dict) else None
        if schema_name != definition.name:
            raise ValueError(
                f"tool/schema name mismatch: definition={definition.name!r} schema={schema_name!r}"
            )
        self._definitions[definition.name] = definition
        return definition

    def get(self, name: str) -> ToolSpec | None:
        return self._definitions.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [definition.schema for definition in self._definitions.values()]

    def definitions(self) -> list[ToolSpec]:
        return list(self._definitions.values())

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._definitions.values())

    def __len__(self) -> int:
        return len(self._definitions)

    def __getitem__(self, name: str) -> ToolSpec:
        return self._definitions[name]


def build_registry() -> ToolRegistry:
    """Build the one explicit production registry.

    The schemas below intentionally mirror the Phase 4 public API byte for
    byte at the field/value level.  Registration is explicit; there is no
    filesystem or plugin discovery.
    """

    return ToolRegistry(
        [
            ToolSpec(
                "time_now",
                "Vrati trenutno vreme i datum.",
                _schema("time_now", "Trenutno vreme i datum.", {}, []),
                time_now,
                timeout_s=5,
            ),
            ToolSpec(
                "reminders_create",
                "Kreiraj podsetnik u Apple Reminders.",
                _schema(
                    "reminders_create",
                    "Napravi novi podsetnik. `due_iso` je ISO datum (npr. 2025-12-01T10:30) — opciono.",
                    {
                        "title": _str_prop("Tekst podsetnika."),
                        "list": _str_prop("Naziv liste (default 'Inbox')."),
                        "due_iso": _str_prop("Rok u ISO formatu, opciono."),
                    },
                    ["title"],
                ),
                reminders_create,
                timeout_s=30,
            ),
            ToolSpec(
                "reminders_list",
                "Listaj aktivne podsetnike iz zadate liste.",
                _schema(
                    "reminders_list",
                    "Prikaži nezavršene podsetnike.",
                    {
                        "list": _str_prop("Naziv liste (default 'Inbox')."),
                        "limit": _int_prop("Maksimum stavki.", default=25),
                    },
                    [],
                ),
                reminders_list,
                timeout_s=25,
            ),
            ToolSpec(
                "calendar_today",
                "Prikaži današnje događaje iz Apple kalendara.",
                _schema(
                    "calendar_today",
                    "Čita današnje evente. Ako `calendar` nije zadat, pita sve kalendare.",
                    {"calendar": _str_prop("Naziv kalendara (opciono).")},
                    [],
                ),
                calendar_today,
                timeout_s=30,
            ),
            ToolSpec(
                "open_app",
                "Otvori macOS aplikaciju po imenu (npr. 'Safari', 'Music').",
                _schema("open_app", "Otvori aplikaciju.", {"name": _str_prop("Ime aplikacije.")}, ["name"]),
                open_app,
                timeout_s=15,
            ),
            ToolSpec(
                "open_url",
                "Otvori URL u podrazumevanom browseru (ili imenovanom, npr. 'Google Chrome').",
                _schema(
                    "open_url",
                    "Otvori URL.",
                    {
                        "url": _str_prop("URL (http/https ili bez prefiksa)."),
                        "browser": _str_prop("Opciono ime aplikacije (npr. 'Google Chrome', 'Safari')."),
                    },
                    ["url"],
                ),
                open_url,
                timeout_s=15,
            ),
            ToolSpec(
                "play_youtube",
                "Otvori Chrome, pretraži YouTube za `query` i pusti prvi video. Koristi za videe, klipove, tutoriale — NE za obične pesme (za to koristi ytm_play).",
                _schema(
                    "play_youtube",
                    "YouTube (web) reprodukcija. Za pesme koristiti ytm_play.",
                    {"query": _str_prop("Naziv klipa / videa / izvođača (ne pesme).")},
                    ["query"],
                ),
                play_youtube,
                timeout_s=60,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_play",
                "Pretraži i pusti verifikovani rezultat u povezanoj namenskoj YT Music browser sesiji. Ako nije povezana, vrati zahtev za prijavu.",
                _schema(
                    "ytm_play",
                    "YouTube Music reprodukcija. Podrazumevani izbor kad korisnik traži pesmu.",
                    {
                        "query": _str_prop(
                            "Naziv pesme / izvođača (opciono — bez toga samo proverava/povezuje YT Music)."
                        )
                    },
                    [],
                ),
                ytm_play,
                timeout_s=120,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_pause",
                "Pauziraj muziku koja trenutno svira (verifikuje efekat i prijavljuje stvarno stanje).",
                _schema("ytm_pause", "Pauziraj reprodukciju.", {}, []),
                ytm_pause,
                timeout_s=90,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_resume",
                "Nastavi reprodukciju muzike (verifikuje efekat i prijavljuje stvarno stanje).",
                _schema("ytm_resume", "Nastavi reprodukciju.", {}, []),
                ytm_resume,
                timeout_s=90,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_next",
                "Sledeća pesma (verifikuje efekat i prijavljuje stvarno stanje).",
                _schema("ytm_next", "Sledeća pesma.", {}, []),
                ytm_next,
                timeout_s=90,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_previous",
                "Prethodna pesma (verifikuje efekat i prijavljuje stvarno stanje).",
                _schema("ytm_previous", "Prethodna pesma.", {}, []),
                ytm_previous,
                timeout_s=90,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_volume_up",
                "Pojačaj samo YT Music player za zadati procenat (ne menja macOS sistemski zvuk).",
                _schema(
                    "ytm_volume_up",
                    "Pojačaj samo YT Music player. Bez amount koristi 10%; amount je 1-100.",
                    {
                        "amount": {
                            **_int_prop("Procenat povećanja, 1-100.", default=10),
                            "minimum": 1,
                            "maximum": 100,
                        }
                    },
                    [],
                ),
                ytm_volume_up,
                timeout_s=60,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_volume_down",
                "Smanji samo YT Music player za zadati procenat (ne menja macOS sistemski zvuk).",
                _schema(
                    "ytm_volume_down",
                    "Smanji samo YT Music player. Bez amount koristi 10%; amount je 1-100.",
                    {
                        "amount": {
                            **_int_prop("Procenat smanjenja, 1-100.", default=10),
                            "minimum": 1,
                            "maximum": 100,
                        }
                    },
                    [],
                ),
                ytm_volume_down,
                timeout_s=60,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_volume_set",
                "Postavi samo YT Music player na procenat 0-100 (ne menja macOS sistemski zvuk).",
                _schema(
                    "ytm_volume_set",
                    "Postavi YT Music HTML media element na level 0-100.",
                    {"level": {**_int_prop("Ciljni procenat, 0-100."), "minimum": 0, "maximum": 100}},
                    ["level"],
                ),
                ytm_volume_set,
                timeout_s=60,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_volume_mute",
                "Utišaj / vrati samo YT Music player (ne menja macOS sistemski mute).",
                _schema("ytm_volume_mute", "Utišaj ili vrati samo YT Music player.", {}, []),
                ytm_volume_mute,
                timeout_s=60,
                suppresses_speech=True,
            ),
            ToolSpec(
                "ytm_status",
                "Prikaži samo stanje iz namenske YT Music browser sesije; generički macOS now-playing nije dokaz.",
                _schema("ytm_status", "Status reprodukcije.", {}, []),
                ytm_status,
                timeout_s=30,
            ),
            ToolSpec(
                "web_search",
                "Pretraži web (DuckDuckGo) i vrati top rezultate.",
                _schema(
                    "web_search",
                    "Web pretraga.",
                    {
                        "query": _str_prop("Upit."),
                        "max_results": _int_prop("Maks rezultata.", default=5),
                    },
                    ["query"],
                ),
                web_search,
                timeout_s=20,
            ),
            ToolSpec(
                "read_clipboard",
                "Pročitaj sistemski clipboard.",
                _schema("read_clipboard", "Čita clipboard.", {}, []),
                read_clipboard,
                timeout_s=10,
            ),
            ToolSpec(
                "write_clipboard",
                "Zapiši tekst u sistemski clipboard.",
                _schema(
                    "write_clipboard",
                    "Piše u clipboard.",
                    {"text": _str_prop("Tekst za clipboard.")},
                    ["text"],
                ),
                write_clipboard,
                timeout_s=10,
            ),
            ToolSpec(
                "system_volume",
                "Podesi zvuk sistema (level 0-100 ili mute/unmute).",
                _schema(
                    "system_volume",
                    "Kontrola zvuka.",
                    {
                        "level": _int_prop("0-100."),
                        "mute": _bool_prop("True = utišaj, False = uključi."),
                    },
                    [],
                ),
                system_volume,
                timeout_s=15,
            ),
            ToolSpec(
                "kilo_run",
                "Pošalji kod/terminal zadatak Kilo agentu (sa strožim profilom dozvola).",
                _schema(
                    "kilo_run",
                    "Pokreće `kilo run --auto` sa zadatim promptom. Vraća stdout+stderr i exit code. Koristi za kod/terminal poslove.",
                    {
                        "prompt": _str_prop("Detaljan opis zadatka."),
                        "cwd": _str_prop("Radni direktorijum (opciono)."),
                        "max_duration_s": _int_prop("Maks sekundi.", default=180),
                    },
                    ["prompt"],
                ),
                kilo_run_tool,
                timeout_s=None,
            ),
        ]
    )


DEFAULT_REGISTRY = build_registry()


def all_schemas() -> list[dict[str, Any]]:
    return DEFAULT_REGISTRY.schemas()


def get(name: str) -> ToolSpec | None:
    return DEFAULT_REGISTRY.get(name)
