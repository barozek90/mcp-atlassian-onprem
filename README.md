# mcp-atlassian-onprem

Serwer MCP (stdio) dla **Jiry** i **Confluence** w wersji on-prem (Server / Data Center).

- Domyślnie **tylko odczyt**.
- Zapis włącza się **osobno** dla każdej usługi:
  - `--jira-rw`: tworzenie zgłoszeń i praca na ticketach,
  - `--confluence-rw`: tworzenie i edycja artykułów.
- Serwer działa wyłącznie w ramach podanych projektów Jira i przestrzeni Confluence.
  Bez nich się nie uruchomi.
- Nie ma żadnych operacji administracyjnych, także na własnym koncie.

## Instalacja

```bash
uv sync
```

## Konfiguracja

| Zmienna | Opis |
|---|---|
| `JIRA_URL` | Adres Jiry, taki jak "Base URL" w jej konfiguracji, np. `https://jira.example.com` |
| `JIRA_PROJECTS` | **Wymagane.** Klucze projektów po przecinku, np. `OPS` lub `OPS,DEV` |
| `JIRA_TOKEN` | Personal Access Token (Jira 8.14+) |
| `JIRA_USERNAME`, `JIRA_PASSWORD` | Alternatywa dla tokenu w starszych wersjach |
| `JIRA_CA_BUNDLE` | Plik PEM z firmowym CA |
| `CONFLUENCE_URL` | Adres Confluence (razem ze ścieżką, np. `/confluence`, jeśli jest) |
| `CONFLUENCE_SPACES` | **Wymagane.** Klucze przestrzeni po przecinku |
| `CONFLUENCE_TOKEN` | Personal Access Token (Confluence 7.9+) |
| `CONFLUENCE_USERNAME`, `CONFLUENCE_PASSWORD` | Alternatywa dla tokenu |
| `CONFLUENCE_CA_BUNDLE` | Plik PEM z firmowym CA |
| `JIRA_MODE` | `ro` (domyślnie) lub `rw`, to samo co `--jira-rw` |
| `CONFLUENCE_MODE` | `ro` (domyślnie) lub `rw`, to samo co `--confluence-rw` |
| `ATLASSIAN_MAX_ATTACHMENT_BYTES` | Limit załącznika, domyślnie 5 MiB |
| `ATLASSIAN_TIMEOUT` | Timeout żądania w sekundach, domyślnie 30 |
| `ATLASSIAN_ALLOW_HTTP` | `1` pozwala na nieszyfrowane `http://` |

Można skonfigurować tylko jedną z usług: wystarczy pominąć `..._URL` drugiej.
Projekt i przestrzeń można też podać flagami `--jira-project` i `--confluence-space`,
które mają pierwszeństwo przed zmiennymi.

## Podłączenie do klienta MCP

Przykład dla Claude Code (`.mcp.json` w projekcie). Tokeny są brane ze zmiennych
środowiskowych, więc nie trafiają do pliku:

```json
{
  "mcpServers": {
    "atlassian": {
      "command": "uv",
      "args": [
        "run", "--directory", "/sciezka/do/repo", "mcp-atlassian-onprem",
        "--jira-project", "OPS",
        "--confluence-space", "DOC"
      ],
      "env": {
        "JIRA_URL": "https://jira.example.com",
        "JIRA_TOKEN": "${JIRA_TOKEN}",
        "CONFLUENCE_URL": "https://wiki.example.com",
        "CONFLUENCE_TOKEN": "${CONFLUENCE_TOKEN}"
      }
    }
  }
}
```

Tryb zapisu: dopisz do `args` `"--jira-rw"` i/lub `"--confluence-rw"`.

## Uprawnienia w Claude Code

W [`examples/claude-settings.json`](examples/claude-settings.json) jest gotowa lista:
narzędzia tylko czytające są w `allow`, a wszystkie zapisujące w `ask`. Przenieś sekcję
`permissions` do `.claude/settings.json` projektu lub do `~/.claude/settings.json`.
Nazwy zakładają, że serwer w konfiguracji MCP nazywa się `atlassian`.

## Tworzenie zgłoszeń

Wymagane pola są inne w każdym projekcie i typie zgłoszenia, więc serwer pobiera je
z Jiry za każdym razem:

1. `jira_get_create_fields(project_key="OPS")` zwraca typy zgłoszeń.
2. `jira_get_create_fields(project_key="OPS", issue_type="Task")` zwraca pola wymagane,
   wymagane z wartością domyślną i opcjonalne, z dozwolonymi wartościami i przykładem formatu.
3. `jira_create_issue(...)`: jeśli brakuje wymaganych pól, nic nie powstaje, a odpowiedź
   zawiera `missing_required_fields`. Jeśli Jira odrzuci wartość, odpowiedź zawiera
   `jira_errors`.

Walidatory workflow i wtyczki mogą wymagać więcej, niż Jira zgłasza w metadanych.
Wychodzi to dopiero w `jira_errors`.

## Edycja zgłoszeń, sprinty i linki

Wszystko poniżej wymaga `--jira-rw`.

- **Pola**: `jira_get_edit_fields` pokazuje, co można zmienić, a `jira_update_issue` to
  zapisuje, np. `fields={"priority": {"name": "High"}}`. Podana wartość zastępuje obecną
  (także całe listy, np. `labels`), `null` czyści pole. Nie da się zmienić zgłaszającego,
  projektu, typu, nadrzędnego zgłoszenia ani poziomu bezpieczeństwa.
- **Przypisanie**: `jira_assign_issue`.
- **Sprinty**: `jira_list_sprints` pokazuje aktywne i przyszłe sprinty z tablic scrum
  dozwolonych projektów. `jira_add_to_sprint` przyjmuje tylko ID z tej listy,
  `jira_move_to_backlog` wyjmuje zgłoszenie ze sprintu.
- **Linki**: `jira_link_issues(issue_key="OPS-1", relation="blocks", target_key="OPS-2")`
  czyta się "OPS-1 blocks OPS-2". Dostępne relacje zwraca `jira_list_link_types`. Aby zmienić
  typ linku, usuń go (`jira_delete_link`, ID z `jira_get_issue`) i utwórz nowy.

## Narzędzia

| Narzędzie | Tryb | Opis |
|---|---|---|
| `jira_search` | ro | Wyszukiwanie JQL w dozwolonych projektach |
| `jira_get_issue` | ro | Szczegóły zgłoszenia, linki, lista załączników |
| `jira_get_comments` | ro | Komentarze |
| `jira_get_create_fields` | rw | Typy zgłoszeń i pola wymagane do ich utworzenia |
| `jira_create_issue` | rw | Nowe zgłoszenie w dozwolonym projekcie |
| `jira_read_attachment` | rw | Treść załącznika: tekst, PDF, obraz |
| `jira_add_comment` | rw | Dodanie komentarza, opcjonalnie z ograniczoną widocznością |
| `jira_list_transitions` | rw | Dostępne zmiany statusu |
| `jira_transition_issue` | rw | Zmiana statusu, opcjonalnie z komentarzem i resolution |
| `jira_find_assignable_users` | rw | Kogo można przypisać |
| `jira_assign_issue` | rw | Przypisanie lub odpięcie osoby |
| `jira_assign_to_me` | rw | Przypisanie do konta serwera |
| `jira_get_edit_fields` | rw | Pola, które można zmienić w zgłoszeniu |
| `jira_update_issue` | rw | Zmiana pól, np. priorytetu |
| `jira_list_sprints` | rw | Aktywne i przyszłe sprinty |
| `jira_add_to_sprint` | rw | Dodanie zgłoszenia do sprintu |
| `jira_move_to_backlog` | rw | Przeniesienie zgłoszenia do backlogu |
| `jira_list_link_types` | rw | Typy linków |
| `jira_link_issues` | rw | Połączenie dwóch zgłoszeń |
| `jira_delete_link` | rw | Usunięcie linku |
| `confluence_search` | ro | Wyszukiwanie tekstem lub CQL |
| `confluence_get_page` | ro | Artykuł jako Markdown |
| `confluence_get_children` | ro | Podstrony |
| `confluence_get_comments` | ro | Komentarze do strony |
| `confluence_list_attachments` | ro | Lista załączników (bez treści) |
| `confluence_create_page` | rw | Nowa strona w dozwolonej przestrzeni |
| `confluence_update_page` | rw | Edycja strony lub wpisu na blogu: fragment, całość lub tytuł |

Treść zapisywana do Confluence jest w **storage format** (XHTML), nie w Markdown.
Edycja wymaga numeru przeczytanej wersji, więc nie nadpisze cudzych, nowszych zmian.
Bezpieczniej jest podmieniać fragment (`old_text` / `new_text`) niż całą treść, bo
reszta strony i jej makra zostają bez zmian.

## Zabezpieczenia

1. **Biała lista HTTP** (`http.py`): każde żądanie musi pasować do jawnej reguły
   (metoda + ścieżka). W trybie `ro` są to wyłącznie `GET`. Tryb `rw` dodaje tylko te
   operacje zapisu:
   - Jira: utworzenie zgłoszenia, komentarz, zmiana statusu, przypisanie, edycja pól,
     przeniesienie do sprintu lub backlogu, utworzenie i usunięcie linku,
   - Confluence: utworzenie i edycja treści.

   Jedyne dozwolone usuwanie to link między zgłoszeniami. Niedostępne są: usuwanie
   zgłoszeń i treści, zarządzanie sprintami, tablicami, typami linków, projektami
   i przestrzeniami, uprawnienia i restrykcje, użytkownicy, grupy, tokeny, hasła
   i ustawienia konta.

   To, których pól nie wolno edytować, sprawdza kod narzędzia (`jira_edit.py`), a nie biała
   lista. Tak samo zakres sprintów i linków (`jira_agile.py`, `jira_links.py`).
2. **Narzędzia zapisu nie są rejestrowane** dla usługi w trybie `ro`.
3. **Zakres**: zapytania JQL/CQL są opakowywane warunkiem na projekt lub przestrzeń,
   a wyniki dodatkowo filtrowane po tym, co faktycznie zwrócił serwer. Zgłoszenie
   przeniesione do innego projektu jest odrzucane. Linki i podzadania spoza zakresu
   są ukrywane.
4. **Token** trafia tylko pod skonfigurowany adres, a przekierowania nie są śledzone.

### Czego to nie załatwia

- Ograniczenia obowiązują **w tym serwerze**, nie na samym tokenie. PAT w Server /
  Data Center ma pełne uprawnienia konta. Agent z dostępem do powłoki i do tokenu może
  wywołać API z pominięciem MCP. Najlepiej użyć osobnego konta technicznego z minimalnymi
  uprawnieniami.
- Treść zgłoszeń, stron i załączników piszą inni ludzie i może zawierać próby
  *prompt injection*. W trybie `rw` zatwierdzaj operacje zapisu ręcznie.
- Zmiana statusu uruchamia to, co jest skonfigurowane w workflow (np. post-functions).
- Tablica scrum może obejmować kilka projektów, więc jej sprinty bywają wspólne także
  z projektami spoza zakresu.
- Zapisywana treść strony nie jest filtrowana. Makra (np. include z innej przestrzeni
  albo HTML, jeśli włączył je administrator) zadziałają przy wyświetlaniu.
- Tekst zgłoszeń i stron z dozwolonego zakresu może cytować informacje z innych projektów.

## Rozwój

`.gitlab-ci.yml` uruchamia na GitLabie lint, sprawdzenie formatowania i testy.
Niczego nie buduje ani nie wdraża. Te same kroki lokalnie:

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
```

Nowy endpoint wymaga dopisania reguły w `http.py` oraz testu.
