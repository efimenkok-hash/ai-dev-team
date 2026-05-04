from enum import Enum


class State(str, Enum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    PM = "PM"
    ARCHITECT = "ARCHITECT"
    WRITER = "WRITER"
    REVIEW = "REVIEW"
    TEST = "TEST"
    QA = "QA"
    FIX = "FIX"
    SUCCESS = "SUCCESS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


TERMINAL_STATES = {
    State.SUCCESS,
    State.FAIL,
    State.BLOCKED,
}

ALLOWED_TRANSITIONS = {
    State.IDLE: {State.PLANNING},
    State.PLANNING: {State.PM, State.BLOCKED},
    State.PM: {State.ARCHITECT, State.BLOCKED},
    State.ARCHITECT: {State.WRITER, State.BLOCKED},
    State.WRITER: {State.REVIEW, State.BLOCKED},
    State.REVIEW: {State.TEST, State.FIX, State.FAIL},
    State.TEST: {State.QA, State.FIX, State.FAIL},
    State.QA: {State.SUCCESS, State.FIX, State.FAIL},
    State.FIX: {State.REVIEW, State.FAIL},
    State.SUCCESS: set(),
    State.FAIL: set(),
    State.BLOCKED: set(),
}

MAX_TOTAL_STEPS = 25
MAX_TOTAL_AGENT_CALLS = 40
MAX_REVIEW_FIX_LOOPS = 3
MAX_TEST_FIX_LOOPS = 3
MAX_QA_FIX_LOOPS = 2
STATE_MAX_RETRY = 2


def can_transition(current: State, nxt: State) -> bool:
    return nxt in ALLOWED_TRANSITIONS[current]


def is_terminal(state: State) -> bool:
    return state in TERMINAL_STATES
