# rackbrain/adapters/ilom_parser.py

import re
from typing import List
from rackbrain.core.models import IlomProblem

DAY_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s")

def extract_ilom_problems(output: str) -> List[IlomProblem]:
    """
    Parse 'show System/Open_Problems' output into a list of IlomProblem objects.

    Example input section:

        Open Problems (2)
        Date/Time                 Subsystems          Component
        ------------------------  ------------------  ------------
        Fri Nov 21 23:27:35 2025  System              /System (Host System)
                The power supplies are not providing redundant availability. (...)
        Fri Nov 21 23:28:32 2025  Power               PS1 (Power Supply 1)
                A loss of AC input power to a power supply has been detected. (...)

    We detect problem rows by weekday at start of line, then treat the following
    indented lines as the description for that problem until the next problem row.
    """
    problems: List[IlomProblem] = []
    if not output:
        return problems

    lines = output.splitlines()

    # Find header line with "Date/Time" and "Component"
    header_idx = None
    for idx, line in enumerate(lines):
        if "Date/Time" in line and "Component" in line:
            header_idx = idx
            break

    if header_idx is None:
        return problems

    # Skip header + separator line (dashes)
    i = header_idx + 1
    # skip blank lines and the dashed separator
    while i < len(lines):
        stripped = lines[i].rstrip("\n")
        if stripped and set(stripped.replace(" ", "")) == {"-"}:
            i += 1
            break
        i += 1

    current: IlomProblem = None  # type: ignore

    while i < len(lines):
        stripped = lines[i].rstrip("\n")

        if not stripped.strip():
            # blank line, just skip
            i += 1
            continue

        if DAY_RE.match(stripped):
            # New problem row: close out previous if any
            if current is not None:
                problems.append(current)

            # Split into columns by 2+ spaces; component is last column
            parts = re.split(r"\s{2,}", stripped.strip())
            if len(parts) >= 3:
                component = parts[-1].strip()
            else:
                component = stripped.strip()

            current = IlomProblem(component=component, description="")
        else:
            # Continuation / description line for current problem
            if current is not None:
                text_line = stripped.lstrip()
                if current.description:
                    current.description += "\n" + text_line
                else:
                    current.description = text_line

        i += 1

    # Flush last problem
    if current is not None:
        problems.append(current)

    return problems