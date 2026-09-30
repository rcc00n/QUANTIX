"""Logical ordering only for exactly equal event timestamps; original times stay intact."""

from sqlalchemy import case

LIFECYCLE_RANK = {
    "CANDIDATE": 0,
    "ELIGIBILITY_CHECK": 1,
    "SCHEDULED": 2,
    "RECHECKING": 3,
    "EXECUTING": 4,
    "COMPLETED": 5,
    "PARTIAL": 5,
    "MISSED": 5,
    "FAILED": 5,
    "EXPIRED": 5,
}


def lifecycle_rank(column):
    return case(LIFECYCLE_RANK, value=column, else_=99)
