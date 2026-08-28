#!/usr/bin/env python3

import argparse
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_URL_PATTERN = re.compile(
    r"""DEFAULT_URL\s*=\s*(['"])(.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ClassifierLink:
    viewer_url: str
    title: str | None = None
    scoring: str | None = None
    rounds: str | None = None


def load_html(
    source: str,
    session: requests.Session,
) -> tuple[str, str]:
    """
    Load HTML from either a webpage URL or a local HTML file.

    Returns:
        A tuple containing:
            1. The HTML document text.
            2. The base URL used to resolve relative links.
    """
    if source.startswith(("http://", "https://")):
        response = session.get(
            source,
            timeout=30,
            allow_redirects=True,
        )
        response.raise_for_status()

        return response.text, response.url

    html_path = Path(source).expanduser().resolve()

    if not html_path.is_file():
        raise FileNotFoundError(
            f"HTML file does not exist: {html_path}"
        )

    return (
        html_path.read_text(encoding="utf-8"),
        html_path.as_uri(),
    )


def clean_text(value: str | None) -> str | None:
    """
    Normalize whitespace in text extracted from HTML.
    """
    if value is None:
        return None

    value = re.sub(r"\s+", " ", value).strip()

    return value or None


def compact_filename_piece(value: str | None) -> str | None:
    """
    Turn a human-readable text field into a compact filename-safe piece.

    Examples:
        Hillbillton Drill -> HillbilltonDrill
        Fixed Time        -> FixedTime
        Fluffy's Revenge 1 -> FluffysRevenge1
    """
    value = clean_text(value)

    if not value:
        return None

    # Remove anything that is not a letter or digit.
    value = re.sub(r"[^A-Za-z0-9]+", "", value)

    return value or None


def rounds_filename_piece(value: str | None) -> str | None:
    """
    Turn a rounds value into a filename suffix.

    Examples:
        12 -> 12rounds
        1  -> 1round
        -  -> None
    """
    value = clean_text(value)

    if not value:
        return None

    match = re.search(r"\d+", value)

    if not match:
        return None

    rounds = int(match.group(0))

    if rounds == 1:
        return "1round"

    return f"{rounds}rounds"


def parse_classifier_metadata_from_card(
    card,
) -> tuple[str | None, str | None, str | None]:
    """
    Extract title, scoring, and rounds from one classifier card.
    """
    title = None
    scoring = None
    rounds = None

    title_element = card.select_one("h6.card-subtitle")

    if title_element is not None:
        title = clean_text(title_element.get_text(" ", strip=True))

    card_text = card.select_one("p.card-text")

    if card_text is not None:
        lines = [
            clean_text(line)
            for line in card_text.get_text("\n", strip=True).splitlines()
        ]

        for line in lines:
            if not line:
                continue

            scoring_match = re.match(
                r"^Scoring:\s*(.+)$",
                line,
                flags=re.IGNORECASE,
            )

            if scoring_match:
                scoring = clean_text(scoring_match.group(1))
                continue

            rounds_match = re.match(
                r"^Rounds:\s*(.+)$",
                line,
                flags=re.IGNORECASE,
            )

            if rounds_match:
                rounds = clean_text(rounds_match.group(1))
                continue

    return title, scoring, rounds


def find_view_links(
    html_text: str,
    base_url: str,
) -> list[ClassifierLink]:
    """
    Find View buttons and extract classifier metadata from their card.

    Duplicate URLs are removed while preserving their original order.
    """
    soup = BeautifulSoup(html_text, "html.parser")

    links: list[ClassifierLink] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        button_text = anchor.get_text(strip=True)

        if button_text.casefold() != "view":
            continue

        href = anchor["href"].strip()

        if not href:
            continue

        viewer_url = urljoin(base_url, href)

        if viewer_url in seen_urls:
            continue

        seen_urls.add(viewer_url)

        title = None
        scoring = None
        rounds = None

        card = anchor.find_parent(class_="card")

        if card is not None:
            title, scoring, rounds = parse_classifier_metadata_from_card(
                card,
            )

        links.append(
            ClassifierLink(
                viewer_url=viewer_url,
                title=title,
                scoring=scoring,
                rounds=rounds,
            )
        )

    return links


def extract_pdf_url_from_viewer(
    viewer_html: str,
    viewer_url: str,
) -> str:
    """
    Extract the real PDF URL from a PDF.js viewer page.

    Example found in the viewer HTML:

        var DEFAULT_URL = '/viewer/pdf/99-63.pdf';
    """
    match = DEFAULT_URL_PATTERN.search(viewer_html)

    if not match:
        raise ValueError(
            "Could not find DEFAULT_URL in the PDF.js viewer page."
        )

    pdf_path = match.group(2)

    # Decode HTML entities.
    pdf_path = html.unescape(pdf_path)

    # Decode JavaScript-style escaped forward slashes.
    pdf_path = pdf_path.replace(r"\/", "/")

    # Handle a few common JavaScript escape sequences.
    pdf_path = pdf_path.replace(r"\u002F", "/")
    pdf_path = pdf_path.replace(r"\u003A", ":")
    pdf_path = pdf_path.replace(r"\u0026", "&")

    return urljoin(viewer_url, pdf_path)


def content_is_pdf(content: bytes) -> bool:
    """
    Check whether data begins with the standard PDF signature.
    """
    return content.lstrip().startswith(b"%PDF-")


def filename_from_url(
    url: str,
    fallback_index: int,
) -> str:
    """
    Create the base PDF filename from the final PDF URL.

    Example:
        https://uspsa.org/viewer/pdf/99-28.pdf -> 99-28.pdf
    """
    parsed_url = urlparse(url)
    decoded_path = unquote(parsed_url.path)
    filename = Path(decoded_path).name

    if not filename:
        filename = f"document_{fallback_index:04d}.pdf"

    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    return filename


def append_classifier_metadata_to_filename(
    base_filename: str,
    classifier: ClassifierLink,
) -> str:
    """
    Append classifier title, scoring, and rounds to the original filename.

    Example:
        99-28.pdf + Hillbillton Drill, Comstock, 12
        ->
        99-28_HillbilltonDrill_Comstock_12rounds.pdf
    """
    base_path = Path(base_filename)

    pieces: list[str] = []

    title_piece = compact_filename_piece(classifier.title)
    scoring_piece = compact_filename_piece(classifier.scoring)
    rounds_piece = rounds_filename_piece(classifier.rounds)

    if title_piece:
        pieces.append(title_piece)

    if scoring_piece:
        pieces.append(scoring_piece)

    if rounds_piece:
        pieces.append(rounds_piece)

    if not pieces:
        return base_filename

    return f"{base_path.stem}_{'_'.join(pieces)}{base_path.suffix}"


def unique_path(
    directory: Path,
    filename: str,
) -> Path:
    """
    Return a path that will not overwrite an existing file.
    """
    candidate = directory / filename

    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2

    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"

        if not candidate.exists():
            return candidate

        counter += 1


def describe_non_pdf_response(
    response: requests.Response,
) -> str:
    """
    Build a short diagnostic description for a non-PDF response.
    """
    content_type = response.headers.get(
        "Content-Type",
        "unknown",
    )

    preview = response.content[:300].decode(
        "utf-8",
        errors="replace",
    )

    preview = preview.replace("\n", " ").strip()

    return (
        f"URL: {response.url}\n"
        f"Content-Type: {content_type}\n"
        f"Response preview: {preview!r}"
    )


def request_actual_pdf(
    session: requests.Session,
    initial_url: str,
) -> tuple[requests.Response, str]:
    """
    Request a URL that may be either:

    1. A direct PDF URL, or
    2. A PDF.js HTML viewer page.

    Returns:
        A tuple containing:
            1. The response containing the actual PDF bytes.
            2. The actual PDF URL.
    """
    viewer_response = session.get(
        initial_url,
        timeout=60,
        allow_redirects=True,
        headers={
            "Accept": (
                "application/pdf,"
                "text/html,"
                "application/xhtml+xml,"
                "*/*;q=0.8"
            ),
        },
    )
    viewer_response.raise_for_status()

    if content_is_pdf(viewer_response.content):
        return viewer_response, viewer_response.url

    # The first URL returned something other than a PDF.
    # Attempt to treat it as a PDF.js viewer page.
    actual_pdf_url = extract_pdf_url_from_viewer(
        viewer_html=viewer_response.text,
        viewer_url=viewer_response.url,
    )

    pdf_response = session.get(
        actual_pdf_url,
        timeout=120,
        allow_redirects=True,
        headers={
            "Accept": "application/pdf,*/*;q=0.8",
            "Referer": viewer_response.url,
        },
    )
    pdf_response.raise_for_status()

    if not content_is_pdf(pdf_response.content):
        raise ValueError(
            "The URL extracted from DEFAULT_URL did not return "
            "a valid PDF.\n"
            + describe_non_pdf_response(pdf_response)
        )

    return pdf_response, pdf_response.url


def save_pdf(
    pdf_response: requests.Response,
    actual_pdf_url: str,
    classifier: ClassifierLink,
    output_directory: Path,
    index: int,
) -> Path:
    """
    Save verified PDF data using a temporary file first.
    """
    if not content_is_pdf(pdf_response.content):
        raise ValueError(
            "Refusing to save the response because it does not "
            "begin with the PDF signature."
        )

    base_filename = filename_from_url(
        actual_pdf_url,
        fallback_index=index,
    )

    final_filename = append_classifier_metadata_to_filename(
        base_filename=base_filename,
        classifier=classifier,
    )

    output_path = unique_path(
        output_directory,
        final_filename,
    )

    temporary_path = output_path.with_suffix(
        output_path.suffix + ".part"
    )

    try:
        with temporary_path.open("wb") as output_file:
            output_file.write(pdf_response.content)

        # Verify what was written to disk before renaming it.
        with temporary_path.open("rb") as input_file:
            beginning = input_file.read(16)

        if not content_is_pdf(beginning):
            raise ValueError(
                f"Downloaded file is not a valid PDF: {temporary_path}"
            )

        temporary_path.replace(output_path)

    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return output_path


def download_pdf(
    session: requests.Session,
    classifier: ClassifierLink,
    output_directory: Path,
    index: int,
    total: int,
) -> bool:
    """
    Resolve and download one PDF.

    Returns True on success and False on failure.
    """
    print(f"[{index}/{total}] Processing:")
    print(f"    Viewer URL: {classifier.viewer_url}")

    if classifier.title:
        print(f"    Title:      {classifier.title}")

    if classifier.scoring:
        print(f"    Scoring:    {classifier.scoring}")

    if classifier.rounds:
        print(f"    Rounds:     {classifier.rounds}")

    try:
        pdf_response, actual_pdf_url = request_actual_pdf(
            session=session,
            initial_url=classifier.viewer_url,
        )

        if actual_pdf_url != classifier.viewer_url:
            print(f"    Actual PDF: {actual_pdf_url}")

        output_path = save_pdf(
            pdf_response=pdf_response,
            actual_pdf_url=actual_pdf_url,
            classifier=classifier,
            output_directory=output_directory,
            index=index,
        )

        file_size = output_path.stat().st_size

        print(f"    Saved:      {output_path}")
        print(f"    Size:       {file_size:,} bytes")
        print()

        return True

    except (
        requests.RequestException,
        ValueError,
        OSError,
    ) as error:
        print(f"    ERROR: {error}", file=sys.stderr)
        print(file=sys.stderr)

        return False


def download_all_pdfs(
    source: str,
    output_directory: Path,
) -> int:
    """
    Find all View links and download the associated PDFs.

    Returns:
        Process exit status:
            0: Every PDF downloaded successfully.
            1: One or more downloads failed.
            2: No matching links were found.
    """
    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        html_text, base_url = load_html(
            source=source,
            session=session,
        )

        classifier_links = find_view_links(
            html_text=html_text,
            base_url=base_url,
        )

        if not classifier_links:
            print("No View links were found.")
            return 2

        print(f"Found {len(classifier_links)} View link(s).")
        print()

        successful = 0

        for index, classifier in enumerate(
            classifier_links,
            start=1,
        ):
            if download_pdf(
                session=session,
                classifier=classifier,
                output_directory=output_directory,
                index=index,
                total=len(classifier_links),
            ):
                successful += 1

        failed = len(classifier_links) - successful

        print("Download summary")
        print("----------------")
        print(f"Successful: {successful}")
        print(f"Failed:     {failed}")
        print(f"Output:     {output_directory.resolve()}")

        return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download the actual PDFs behind USPSA classifier View "
            "buttons, including PDF.js viewer links, and include "
            "classifier metadata in each filename."
        )
    )

    parser.add_argument(
        "source",
        help=(
            "Webpage URL or path to a local HTML file containing "
            "the View links."
        ),
    )

    parser.add_argument(
        "-o",
        "--output-directory",
        type=Path,
        default=Path("downloaded_pdfs"),
        help=(
            "Directory in which PDFs will be saved. "
            "Default: downloaded_pdfs"
        ),
    )

    args = parser.parse_args()

    try:
        return download_all_pdfs(
            source=args.source,
            output_directory=args.output_directory,
        )

    except (
        requests.RequestException,
        FileNotFoundError,
        UnicodeError,
        OSError,
    ) as error:
        print(f"Fatal error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())