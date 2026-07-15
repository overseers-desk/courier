#!/usr/bin/env python3
"""
Quick check that the Gmail-style query grammar and the generic IMAP
emitter work correctly, without requiring an IMAP connection.
"""

from datetime import datetime

from courier.query import parse
from courier.query.emit_imap import emit


def test_example_queries():
    """Run example queries from the documentation through parse+emit."""

    print("Testing Gmail-Style Query Translation")
    print("=" * 80)

    now = datetime.now()

    test_cases = [
        {
            "name": "Bare words, one TEXT term each",
            "query": "meeting notes",
            "expected": [b"TEXT", b"meeting", b"TEXT", b"notes"],
        },
        {
            "name": "from: prefix",
            "query": "from:alice@example.com",
            "expected": [b"FROM", b"alice@example.com"],
        },
        {
            "name": "Combined prefixes",
            "query": "from:alice subject:invoice is:unread",
            "expected": [b"FROM", b"alice", b"SUBJECT", b"invoice", b"UNSEEN"],
        },
        {
            "name": "OR operator",
            "query": "from:alice or from:bob",
            "expected": [b"OR", b"FROM", b"alice", b"FROM", b"bob"],
        },
        {
            "name": "Chained OR right-folds to binary pairs",
            "query": "from:a or from:b or from:c",
            "expected": [b"OR", b"FROM", b"a", b"OR", b"FROM", b"b", b"FROM", b"c"],
        },
        {
            "name": "Parentheses group for real (issue #58)",
            "query": "after:2026-07-13 (ticket OR booking)",
            "expected_length": 7,
        },
        {
            "name": "Negation with dash",
            "query": "-from:alice",
            "expected": [b"NOT", b"FROM", b"alice"],
        },
        {
            "name": "Standalone keyword: all",
            "query": "all",
            "expected": [b"ALL"],
        },
        {
            "name": "Empty query matches all",
            "query": "",
            "expected": [b"ALL"],
        },
        {
            "name": "Raw IMAP passthrough",
            "query": 'imap:OR TEXT "Edinburgh" TEXT "Berlin"',
            "expected": [b"OR", b"TEXT", b"Edinburgh", b"TEXT", b"Berlin"],
        },
    ]

    passed = 0
    failed = 0

    for test in test_cases:
        print(f"\n{test['name']}")
        print("-" * 80)
        print(f"Query: {test['query']!r}")

        try:
            emission = emit(parse(test["query"]), now=now)
            result = emission.criteria
            print(f"Criteria: {result}")
            if emission.report.approximations:
                print(f"Approximations: {emission.report.approximations}")

            if "expected" in test:
                if result == test["expected"]:
                    print("PASS - Exact match")
                    passed += 1
                else:
                    print(f"FAIL - Expected {test['expected']}")
                    failed += 1
            elif "expected_length" in test:
                if len(result) == test["expected_length"]:
                    print(f"PASS - Correct length ({len(result)} atoms)")
                    passed += 1
                else:
                    print(
                        f"FAIL - Expected length {test['expected_length']}, "
                        f"got {len(result)}"
                    )
                    failed += 1
        except Exception as e:
            print(f"ERROR - {e}")
            failed += 1

    print("\n" + "=" * 80)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 80)

    return failed == 0


if __name__ == "__main__":
    success = test_example_queries()
    exit(0 if success else 1)
