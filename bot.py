import asyncio
import logging
import os
import random
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    Update,
    WebAppInfo,
)

from config import BOT_TOKEN, WEBHOOK_SECRET


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("rodas-friend-zone-bingo")


# ============================================================
# CONFIGURATION
# ============================================================

PORT = int(os.getenv("PORT", "10000"))

WEB_DIR = Path(__file__).resolve().parent / "web"

BET_AMOUNT = 10

# ------------------------------------------------------------
# IMPORTANT:
# This is the ONLY cartela/game starting countdown.
#
# It does NOT start when the game is created.
# It starts when the FIRST PLAYER selects a cartela.
# ------------------------------------------------------------

ROUND_COUNTDOWN_SECONDS = 30

# Time between called Bingo numbers.
DRAW_INTERVAL = 3

# Winner/result screen duration.
RESULT_SECONDS = 30

# Winner receives 75% of total bets.
PRIZE_PERCENT = 0.75

CARTELA_MIN = 1
CARTELA_MAX = 100

WEBHOOK_PATH = "/telegram/webhook"

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://rodasfriendzone.onrender.com",
).rstrip("/")

WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"


# ============================================================
# BOT / DISPATCHER
# ============================================================

bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()


# ============================================================
# GLOBAL GAME STATE
# ============================================================

games = {}

game_lock = asyncio.Lock()

next_game_number = 1

game_loop_task = None


# ============================================================
# BASIC HELPERS
# ============================================================

def now():
    return time.time()


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_user_id(request):
    """
    Gets Telegram user ID sent by the Mini App.
    """

    value = (
        request.headers.get("X-User-ID")
        or request.headers.get("X-Telegram-User-ID")
        or request.query.get("user_id")
        or "guest"
    )

    return str(value)


def get_user_name(request):
    return (
        request.headers.get("X-User-Name")
        or request.query.get("user_name")
        or "Player"
    )


def generate_game_number():
    global next_game_number

    number = next_game_number

    next_game_number += 1

    return number


# ============================================================
# CARTELA GENERATION
# ============================================================

def generate_cartela(cartela_number=None):
    """
    Generates a deterministic Bingo card for each cartela.

    Cartela 35 is the exact card shown in the user's example.
    """

    cartela_number = safe_int(
        cartela_number,
        1,
    )

    # --------------------------------------------------------
    # EXACT CARTELA 35
    # --------------------------------------------------------

    if cartela_number == 35:

        return [
            [9, 28, 36, 47, 61],
            [6, 27, 41, 50, 65],
            [13, 25, "FREE", 48, 73],
            [3, 17, 38, 55, 70],
            [14, 21, 31, 60, 68],
        ]

    # --------------------------------------------------------
    # OTHER CARTELAS
    # --------------------------------------------------------

    rng = random.Random(
        50000 + cartela_number
    )

    ranges = {
        "B": list(range(1, 16)),
        "I": list(range(16, 31)),
        "N": list(range(31, 46)),
        "G": list(range(46, 61)),
        "O": list(range(61, 76)),
    }

    columns = ["B", "I", "N", "G", "O"]

    column_values = {}

    for column in columns:

        values = ranges[column].copy()

        rng.shuffle(values)

        column_values[column] = values[:5]

    board = []

    for row in range(5):

        current_row = []

        for column in columns:

            if column == "N" and row == 2:

                current_row.append("FREE")

            else:

                current_row.append(
                    column_values[column][row]
                )

        board.append(current_row)

    return board


# ============================================================
# BINGO PATTERNS
# ============================================================

def cell_is_complete(
    value,
    called,
):
    return (
        value == "FREE"
        or value in called
    )


def check_cells_complete(
    board,
    cells,
    called,
):

    for row, col in cells:

        value = board[row][col]

        if not cell_is_complete(
            value,
            called,
        ):
            return False

    return True


def pattern_cells(
    board,
    pattern_name,
):

    cells = []

    # --------------------------------------------------------
    # HORIZONTAL
    # --------------------------------------------------------

    if pattern_name.startswith("horizontal-"):

        row = safe_int(
            pattern_name.split("-")[1],
            0,
        ) - 1

        if 0 <= row < 5:

            for col in range(5):

                cells.append(
                    [row, col]
                )

    # --------------------------------------------------------
    # VERTICAL
    # --------------------------------------------------------

    elif pattern_name.startswith("vertical-"):

        col = safe_int(
            pattern_name.split("-")[1],
            0,
        ) - 1

        if 0 <= col < 5:

            for row in range(5):

                cells.append(
                    [row, col]
                )

    # --------------------------------------------------------
    # DIAGONALS
    # --------------------------------------------------------

    elif pattern_name == "diagonal-main":

        for i in range(5):

            cells.append(
                [i, i]
            )

    elif pattern_name == "diagonal-other":

        for i in range(5):

            cells.append(
                [i, 4 - i]
            )

    # --------------------------------------------------------
    # FOUR CORNERS
    # --------------------------------------------------------

    elif pattern_name == "four-corners":

        cells = [
            [0, 0],
            [0, 4],
            [4, 0],
            [4, 4],
        ]

    # --------------------------------------------------------
    # CENTER FOUR
    # --------------------------------------------------------

    elif pattern_name == "center-four":

        cells = [
            [1, 1],
            [1, 3],
            [3, 1],
            [3, 3],
        ]

    # --------------------------------------------------------
    # T CORNERS
    # --------------------------------------------------------

    elif pattern_name == "t-corners":

        cells = [
            [0, 0],
            [0, 1],
            [0, 2],
            [0, 3],
            [0, 4],

            [1, 0],
            [1, 4],

            [2, 0],
            [2, 4],

            [3, 0],
            [3, 4],

            [4, 0],
            [4, 4],
        ]

    # --------------------------------------------------------
    # CENTER T
    # --------------------------------------------------------

    elif pattern_name == "center-t":

        cells = [
            [2, 0],
            [2, 1],
            [2, 2],
            [2, 3],
            [2, 4],

            [0, 2],
            [1, 2],
            [3, 2],
            [4, 2],
        ]

    return cells


def winning_patterns_for_board(
    board,
    called_numbers,
):

    called = set(
        called_numbers
    )

    patterns = []

    # --------------------------------------------------------
    # HORIZONTAL
    # --------------------------------------------------------

    for row in range(5):

        cells = [
            [row, col]
            for col in range(5)
        ]

        if check_cells_complete(
            board,
            cells,
            called,
        ):

            patterns.append(
                f"horizontal-{row + 1}"
            )

    # --------------------------------------------------------
    # VERTICAL
    # --------------------------------------------------------

    for col in range(5):

        cells = [
            [row, col]
            for row in range(5)
        ]

        if check_cells_complete(
            board,
            cells,
            called,
        ):

            patterns.append(
                f"vertical-{col + 1}"
            )

    # --------------------------------------------------------
    # DIAGONAL MAIN
    # --------------------------------------------------------

    main_diagonal = [
        [i, i]
        for i in range(5)
    ]

    if check_cells_complete(
        board,
        main_diagonal,
        called,
    ):

        patterns.append(
            "diagonal-main"
        )

    # --------------------------------------------------------
    # DIAGONAL OTHER
    # --------------------------------------------------------

    other_diagonal = [
        [i, 4 - i]
        for i in range(5)
    ]

    if check_cells_complete(
        board,
        other_diagonal,
        called,
    ):

        patterns.append(
            "diagonal-other"
        )

    # --------------------------------------------------------
    # FOUR CORNERS
    # --------------------------------------------------------

    four_corners = [
        [0, 0],
        [0, 4],
        [4, 0],
        [4, 4],
    ]

    if check_cells_complete(
        board,
        four_corners,
        called,
    ):

        patterns.append(
            "four-corners"
        )

    # --------------------------------------------------------
    # CENTER FOUR
    # --------------------------------------------------------

    center_four = [
        [1, 1],
        [1, 3],
        [3, 1],
        [3, 3],
    ]

    if check_cells_complete(
        board,
        center_four,
        called,
    ):

        patterns.append(
            "center-four"
        )

    # --------------------------------------------------------
    # T CORNERS
    # --------------------------------------------------------

    t_corners = [
        [0, 0],
        [0, 1],
        [0, 2],
        [0, 3],
        [0, 4],

        [1, 0],
        [1, 4],

        [2, 0],
        [2, 4],

        [3, 0],
        [3, 4],

        [4, 0],
        [4, 4],
    ]

    if check_cells_complete(
        board,
        t_corners,
        called,
    ):

        patterns.append(
            "t-corners"
        )

    # --------------------------------------------------------
    # CENTER T
    # --------------------------------------------------------

    center_t = [
        [2, 0],
        [2, 1],
        [2, 2],
        [2, 3],
        [2, 4],

        [0, 2],
        [1, 2],
        [3, 2],
        [4, 2],
    ]

    if check_cells_complete(
        board,
        center_t,
        called,
    ):

        patterns.append(
            "center-t"
        )

    return patterns


def pattern_display_name(pattern):

    names = {
        "four-corners": "Four Corners",
        "center-four": "Center Four",
        "t-corners": "T Corners",
        "center-t": "Center T",
        "diagonal-main": "Diagonal",
        "diagonal-other": "Diagonal",
    }

    if pattern in names:

        return names[pattern]

    if pattern.startswith("horizontal-"):

        return "Horizontal Line"

    if pattern.startswith("vertical-"):

        return "Vertical Line"

    return pattern.replace(
        "-",
        " ",
    ).title()


# ============================================================
# BINGO GAME CLASS
# ============================================================

class BingoGame:

    def __init__(
        self,
        game_number,
    ):

        self.game_number = game_number

        # ----------------------------------------------------
        # GAME PHASE
        # ----------------------------------------------------

        self.phase = "selection"

        # ----------------------------------------------------
        # IMPORTANT TIMER
        #
        # None means nobody has selected a cartela yet.
        #
        # The timer begins only when the first cartela
        # is selected.
        # ----------------------------------------------------

        self.round_started_at = None

        self.round_seconds = (
            ROUND_COUNTDOWN_SECONDS
        )

        # ----------------------------------------------------
        # PLAYERS
        #
        # user_id -> cartela number
        #
        # None means player joined but currently has
        # no cartela.
        # ----------------------------------------------------

        self.players = {}

        self.player_names = {}

        self.boards = {}

        self.auto_mode = {}

        self.left_users = set()

        self.disqualified_users = set()

        self.proceeded_users = set()

        # ----------------------------------------------------
        # BINGO NUMBERS
        # ----------------------------------------------------

        self.called_numbers = []

        self.number_pool = list(
            range(1, 76)
        )

        random.shuffle(
            self.number_pool
        )

        self.last_draw_at = None

        self.started_at = None

        self.finished_at = None

        # ----------------------------------------------------
        # WINNER
        # ----------------------------------------------------

        self.winner = None

        self.winner_user_id = None

        self.winner_cartela = None

        self.winner_pattern = None

        self.winner_patterns = []

        self.winner_board = None

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        self.result_started_at = None

    # ========================================================
    # SELECTED PLAYER COUNT
    # ========================================================

    def selected_players(self):

        return [
            user_id
            for user_id, cartela
            in self.players.items()
            if cartela is not None
        ]

    def selected_player_count(self):

        return len(
            self.selected_players()
        )

    # ========================================================
    # TIMER
    # ========================================================

    def has_started_timer(self):

        return (
            self.round_started_at
            is not None
        )

    def start_timer_if_needed(self):

        if (
            self.round_started_at
            is None
            and self.selected_player_count()
            > 0
        ):

            self.round_started_at = now()

            logger.info(
                "Game %s: 30-second countdown started.",
                self.game_number,
            )

    def stop_timer_if_empty(self):

        if self.selected_player_count() == 0:

            self.round_started_at = None

            logger.info(
                "Game %s: countdown stopped because "
                "there are no selected cartelas.",
                self.game_number,
            )

    def round_remaining(self):

        # Nobody selected anything yet.
        if self.round_started_at is None:

            return 0

        elapsed = (
            now()
            - self.round_started_at
        )

        return max(
            0,
            int(
                self.round_seconds
                - elapsed
            ),
        )

    # ========================================================
    # RESULT TIMER
    # ========================================================

    def result_remaining(self):

        if self.result_started_at is None:

            return RESULT_SECONDS

        elapsed = (
            now()
            - self.result_started_at
        )

        return max(
            0,
            int(
                RESULT_SECONDS
                - elapsed
            ),
        )

    # ========================================================
    # ADD PLAYER
    # ========================================================

    def add_player(
        self,
        user_id,
        name=None,
    ):

        user_id = str(
            user_id
        )

        if user_id in self.left_users:

            self.left_users.discard(
                user_id
            )

        if user_id in self.disqualified_users:

            return False

        if user_id not in self.players:

            self.players[user_id] = None

        if name:

            self.player_names[
                user_id
            ] = name

        # Default Auto ON.
        if user_id not in self.auto_mode:

            self.auto_mode[
                user_id
            ] = True

        return True

    # ========================================================
    # SELECT / TOGGLE CARTELA
    # ========================================================

    def select_cartela(
        self,
        user_id,
        cartela_number,
        name=None,
    ):

        user_id = str(
            user_id
        )

        cartela_number = safe_int(
            cartela_number,
            0,
        )

        # ----------------------------------------------------
        # VALIDATE NUMBER
        # ----------------------------------------------------

        if not (
            CARTELA_MIN
            <= cartela_number
            <= CARTELA_MAX
        ):

            return (
                False,
                "Invalid cartela number.",
            )

        # ----------------------------------------------------
        # ONLY SELECTION PHASE
        # ----------------------------------------------------

        if self.phase != "selection":

            return (
                False,
                "Cartela selection is closed.",
            )

        # ----------------------------------------------------
        # DISQUALIFIED
        # ----------------------------------------------------

        if user_id in self.disqualified_users:

            return (
                False,
                "You are disqualified from this round.",
            )

        # ----------------------------------------------------
        # ADD PLAYER
        # ----------------------------------------------------

        if not self.add_player(
            user_id,
            name,
        ):

            return (
                False,
                "You cannot join this round.",
            )

        current_cartela = self.players.get(
            user_id
        )

        # ====================================================
        # SAME NUMBER TOUCHED AGAIN
        #
        # SELECTED -> UNSELECTED
        # ====================================================

        if current_cartela == cartela_number:

            self.players[user_id] = None

            self.boards.pop(
                user_id,
                None,
            )

            self.proceeded_users.discard(
                user_id
            )

            self.stop_timer_if_empty()

            logger.info(
                "Game %s: user %s deselected cartela %s.",
                self.game_number,
                user_id,
                cartela_number,
            )

            return (
                True,
                "Cartela deselected.",
            )

        # ====================================================
        # CHECK WHETHER ANOTHER PLAYER HAS IT
        # ====================================================

        for (
            other_user_id,
            selected_cartela
        ) in self.players.items():

            if (
                other_user_id != user_id
                and selected_cartela
                == cartela_number
            ):

                return (
                    False,
                    "That cartela is already selected.",
                )

        # ====================================================
        # PLAYER CAN ONLY HAVE ONE CARTELA
        #
        # If they had another cartela, replace it.
        # ====================================================

        self.players[user_id] = (
            cartela_number
        )

        self.boards[user_id] = (
            generate_cartela(
                cartela_number
            )
        )

        # Player can proceed again after changing card.
        self.proceeded_users.discard(
            user_id
        )

        # ----------------------------------------------------
        # START TIMER ONLY AFTER FIRST SELECTION
        # ----------------------------------------------------

        self.start_timer_if_needed()

        logger.info(
            "Game %s: user %s selected cartela %s.",
            self.game_number,
            user_id,
            cartela_number,
        )

        return (
            True,
            "Cartela selected.",
        )

    # ========================================================
    # DESELECT
    # ========================================================

    def deselect_cartela(
        self,
        user_id,
    ):

        user_id = str(
            user_id
        )

        if user_id in self.players:

            self.players[user_id] = None

        self.boards.pop(
            user_id,
            None,
        )

        self.proceeded_users.discard(
            user_id
        )

        self.stop_timer_if_empty()

        return True

    # ========================================================
    # PROCEED
    #
    # PROCEED DOES NOT START THE TIMER.
    #
    # The timer already started when the first cartela
    # was selected.
    # ========================================================

    def proceed(
        self,
        user_id,
    ):

        user_id = str(
            user_id
        )

        if user_id not in self.players:

            return (
                False,
                "Please select a cartela first.",
            )

        if self.players[user_id] is None:

            return (
                False,
                "Please select a cartela first.",
            )

        if self.phase != "selection":

            return (
                False,
                "The cartela selection stage is already closed.",
            )

        self.proceeded_users.add(
            user_id
        )

        return (
            True,
            "Proceeding to the Bingo game.",
        )

    # ========================================================
    # LEAVE
    # ========================================================

    def leave(
        self,
        user_id,
    ):

        user_id = str(
            user_id
        )

        self.left_users.add(
            user_id
        )

        self.players.pop(
            user_id,
            None,
        )

        self.player_names.pop(
            user_id,
            None,
        )

        self.boards.pop(
            user_id,
            None,
        )

        self.auto_mode.pop(
            user_id,
            None,
        )

        self.proceeded_users.discard(
            user_id
        )

        self.stop_timer_if_empty()

        logger.info(
            "Game %s: user %s left.",
            self.game_number,
            user_id,
        )

        return True

    # ========================================================
    # AUTO MODE
    # ========================================================

    def set_auto(
        self,
        user_id,
        enabled,
    ):

        user_id = str(
            user_id
        )

        if user_id not in self.players:

            return False

        self.auto_mode[user_id] = bool(
            enabled
        )

        return True

    # ========================================================
    # START BINGO
    # ========================================================

    def start_playing(self):

        selected = self.selected_players()

        if not selected:

            self.phase = "selection"

            self.round_started_at = None

            return False

        self.phase = "playing"

        self.started_at = now()

        self.last_draw_at = now()

        logger.info(
            "Game %s started with %s selected player(s).",
            self.game_number,
            len(selected),
        )

        return True

    # ========================================================
    # DRAW NUMBER
    # ========================================================

    def draw_number(self):

        if not self.number_pool:

            return None

        number = self.number_pool.pop(
            0
        )

        self.called_numbers.append(
            number
        )

        self.last_draw_at = now()

        logger.info(
            "Game %s: number called: %s",
            self.game_number,
            number,
        )

        return number

    # ========================================================
    # SET WINNER
    # ========================================================

    def set_winner(
        self,
        user_id,
        patterns,
    ):

        user_id = str(
            user_id
        )

        if not patterns:

            return False

        board = self.boards.get(
            user_id
        )

        if not board:

            return False

        self.winner_user_id = user_id

        self.winner = (
            self.player_names.get(
                user_id,
                "Player",
            )
        )

        self.winner_cartela = (
            self.players.get(
                user_id
            )
        )

        self.winner_patterns = list(
            patterns
        )

        self.winner_pattern = (
            pattern_display_name(
                patterns[0]
            )
        )

        self.winner_board = [
            row.copy()
            for row in board
        ]

        self.prize = round(
            self.total_bets()
            * PRIZE_PERCENT,
            2,
        )

        self.phase = "result"

        self.result_started_at = now()

        logger.info(
            "Game %s WINNER = %s | cartela=%s | pattern=%s | prize=%s",
            self.game_number,
            self.winner,
            self.winner_cartela,
            self.winner_pattern,
            self.prize,
        )

        return True

    # ========================================================
    # AUTOMATIC WINNER
    #
    # Only players with Auto ON are checked here.
    # ========================================================

    def check_automatic_winner(self):

        if self.phase != "playing":

            return False

        for user_id in list(
            self.selected_players()
        ):

            if user_id in self.disqualified_users:

                continue

            if not self.auto_mode.get(
                user_id,
                True,
            ):

                continue

            board = self.boards.get(
                user_id
            )

            if not board:

                continue

            patterns = (
                winning_patterns_for_board(
                    board,
                    self.called_numbers,
                )
            )

            if patterns:

                return self.set_winner(
                    user_id,
                    patterns,
                )

        return False

    # ========================================================
    # TOTAL BETS
    #
    # Only selected cartelas count.
    # ========================================================

    def total_bets(self):

        return (
            self.selected_player_count()
            * BET_AMOUNT
        )

    # ========================================================
    # SERIALIZE STATE
    # ========================================================

    def serialize(
        self,
        user_id=None,
    ):

        user_id = (
            str(user_id)
            if user_id is not None
            else None
        )

        my_cartela = None

        my_board = None

        my_auto = True

        if user_id:

            my_cartela = (
                self.players.get(
                    user_id
                )
            )

            my_board = (
                self.boards.get(
                    user_id
                )
            )

            my_auto = self.auto_mode.get(
                user_id,
                True,
            )

        # ----------------------------------------------------
        # TAKEN CARTELAS
        # ----------------------------------------------------

        taken_cartelas = [
            cartela
            for cartela
            in self.players.values()
            if cartela is not None
        ]

        # ----------------------------------------------------
        # WINNING CELLS
        # ----------------------------------------------------

        winning_cells = []

        if (
            self.winner_board
            and self.winner_patterns
        ):

            for pattern in (
                self.winner_patterns
            ):

                winning_cells.extend(
                    pattern_cells(
                        self.winner_board,
                        pattern,
                    )
                )

        # ----------------------------------------------------
        # COUNTDOWN
        # ----------------------------------------------------

        remaining = (
            self.round_remaining()
            if self.phase == "selection"
            else 0
        )

        # ----------------------------------------------------
        # RETURN
        # ----------------------------------------------------

        return {

            # BASIC
            "game_number":
                self.game_number,

            "phase":
                self.phase,

            "view_phase":
                self.phase,

            "running":
                self.phase != "finished",

            # ------------------------------------------------
            # SHARED 30 SECOND TIMER
            # ------------------------------------------------

            "selection_remaining":
                remaining,

            "round_remaining":
                remaining,

            "countdown":
                remaining,

            "timer":
                remaining,

            # Kept for compatibility with older frontend.
            "start_remaining":
                0,

            "result_remaining":
                (
                    self.result_remaining()
                    if self.phase == "result"
                    else 0
                ),

            # ------------------------------------------------
            # PLAYERS
            # ------------------------------------------------

            "players":
                self.selected_player_count(),

            "player_count":
                self.selected_player_count(),

            "selected_players":
                self.selected_player_count(),

            # ------------------------------------------------
            # CARTELA
            # ------------------------------------------------

            "my_cartela":
                my_cartela,

            "selected":
                my_cartela,

            "proceeded":
                (
                    user_id in self.proceeded_users
                    if user_id
                    else False
                ),

            "taken_cartelas":
                taken_cartelas,

            "taken":
                taken_cartelas,

            # ------------------------------------------------
            # BOARD
            # ------------------------------------------------

            "my_board":
                my_board,

            "card":
                my_board,

            # ------------------------------------------------
            # AUTO
            # ------------------------------------------------

            "auto":
                my_auto,

            "auto_mode":
                my_auto,

            # ------------------------------------------------
            # CALLED NUMBERS
            # ------------------------------------------------

            "called":
                self.called_numbers,

            "called_numbers":
                self.called_numbers,

            "drawn_numbers":
                self.called_numbers,

            "last_called":
                (
                    self.called_numbers[-1]
                    if self.called_numbers
                    else None
                ),

            # ------------------------------------------------
            # WINNER
            # ------------------------------------------------

            "winner":
                self.winner,

            "winner_user_id":
                self.winner_user_id,

            "winner_cartela":
                self.winner_cartela,

            "winner_pattern":
                self.winner_pattern,

            "winner_patterns":
                self.winner_patterns,

            "winner_board":
                self.winner_board,

            "winning_cells":
                winning_cells,

            # ------------------------------------------------
            # MONEY
            # ------------------------------------------------

            "wallet":
                0,

            "balance":
                0,

            "bet":
                BET_AMOUNT,

            "total_bets":
                self.total_bets(),

            "prize":
                self.prize,

            "derash":
                self.prize,

            # ------------------------------------------------
            # PLAYER STATUS
            # ------------------------------------------------

            "left":
                (
                    user_id in self.left_users
                    if user_id
                    else False
                ),

            "disqualified":
                (
                    user_id
                    in self.disqualified_users
                    if user_id
                    else False
                ),
        }


# ============================================================
# CURRENT GAME
# ============================================================

def get_current_game():

    active = [
        game
        for game in games.values()
        if game.phase != "finished"
    ]

    if active:

        return active[-1]

    game_number = generate_game_number()

    game = BingoGame(
        game_number
    )

    games[game_number] = game

    logger.info(
        "Created Bingo game %s.",
        game_number,
    )

    return game


# ============================================================
# GAME LOOP
# ============================================================

async def game_loop():

    logger.info(
        "Bingo game loop started."
    )

    while True:

        try:

            async with game_lock:

                game = get_current_game()

                # =================================================
                # CARTELA SELECTION
                # =================================================

                if game.phase == "selection":

                    selected_count = (
                        game.selected_player_count()
                    )

                    # ------------------------------------------------
                    # NO PLAYER
                    #
                    # Countdown stays stopped.
                    # ------------------------------------------------

                    if selected_count == 0:

                        game.round_started_at = None

                    # ------------------------------------------------
                    # PLAYER(S) EXIST
                    # ------------------------------------------------

                    else:

                        # First selection starts timer.
                        game.start_timer_if_needed()

                        # ------------------------------------------------
                        # 30 SECONDS FINISHED
                        # ------------------------------------------------

                        if (
                            game.round_remaining()
                            <= 0
                        ):

                            game.start_playing()

                # =================================================
                # PLAYING
                # =================================================

                elif game.phase == "playing":

                    if (
                        game.last_draw_at is None
                        or
                        now()
                        - game.last_draw_at
                        >= DRAW_INTERVAL
                    ):

                        number = (
                            game.draw_number()
                        )

                        if number is not None:

                            logger.info(
                                "Game %s called %s.",
                                game.game_number,
                                number,
                            )

                            # Auto players can win automatically.
                            game.check_automatic_winner()

                        else:

                            logger.warning(
                                "Game %s has no numbers left.",
                                game.game_number,
                            )

                # =================================================
                # RESULT
                # =================================================

                elif game.phase == "result":

                    if (
                        game.result_remaining()
                        <= 0
                    ):

                        game.phase = "finished"

                        game.finished_at = now()

                        logger.info(
                            "Game %s result finished.",
                            game.game_number,
                        )

                # =================================================
                # FINISHED
                # =================================================

                elif game.phase == "finished":

                    new_game_number = (
                        generate_game_number()
                    )

                    new_game = BingoGame(
                        new_game_number
                    )

                    games[
                        new_game_number
                    ] = new_game

                    logger.info(
                        "Created new selection game %s.",
                        new_game_number,
                    )

        except asyncio.CancelledError:

            logger.info(
                "Bingo game loop cancelled."
            )

            raise

        except Exception:

            logger.exception(
                "Unexpected error inside Bingo game loop."
            )

        await asyncio.sleep(
            0.25
        )


# ============================================================
# WEB APP
# ============================================================

async def index_handler(request):

    index_file = (
        WEB_DIR
        / "index.html"
    )

    if not index_file.exists():

        return web.Response(
            text=(
                "<h1>Rodas Friend Zone</h1>"
                "<p>Web app is not available.</p>"
            ),
            content_type="text/html",
        )

    return web.FileResponse(
        index_file
    )


async def health_handler(request):

    return web.json_response(
        {
            "status": "ok",
            "game_count": len(games),
            "webhook_url": WEBHOOK_URL,
        }
    )


# ============================================================
# STATE
# ============================================================

async def state_handler(request):

    user_id = get_user_id(
        request
    )

    async with game_lock:

        game = get_current_game()

        return web.json_response(
            game.serialize(
                user_id
            )
        )


# ============================================================
# REFRESH ALIAS
#
# The frontend can call /api/refresh if desired.
# It does NOT leave the page.
# ============================================================

async def refresh_handler(request):

    user_id = get_user_id(
        request
    )

    async with game_lock:

        game = get_current_game()

        return web.json_response(
            game.serialize(
                user_id
            )
        )


# ============================================================
# API RESPONSE
# ============================================================

def api_game_response(
    game,
    user_id,
    success=True,
    message=None,
    status=200,
):

    payload = game.serialize(
        user_id
    )

    payload["success"] = success

    payload["ok"] = success

    if message is not None:

        payload["message"] = message

    return web.json_response(
        payload,
        status=status,
    )


# ============================================================
# SELECT CARTELA
# ============================================================

async def select_handler(request):

    try:

        data = await request.json()

    except Exception:

        return web.json_response(
            {
                "success": False,
                "ok": False,
                "message": "Invalid JSON.",
            },
            status=400,
        )

    user_id = get_user_id(
        request
    )

    user_name = (
        data.get("name")
        or get_user_name(request)
    )

    cartela_value = data.get(
        "cartela",
        data.get(
            "cartela_number",
            data.get(
                "number"
            ),
        ),
    )

    async with game_lock:

        game = get_current_game()

        if game.phase != "selection":

            return api_game_response(
                game,
                user_id,
                success=False,
                message=(
                    "Cartela selection is closed."
                ),
                status=400,
            )

        # ----------------------------------------------------
        # EMPTY VALUE = DESELECT
        # ----------------------------------------------------

        if (
            cartela_value is None
            or cartela_value == ""
        ):

            if user_id in game.players:

                game.deselect_cartela(
                    user_id
                )

            return api_game_response(
                game,
                user_id,
                success=True,
                message=(
                    "Cartela deselected."
                ),
            )

        # ----------------------------------------------------
        # SELECT / TOGGLE
        # ----------------------------------------------------

        success, message = (
            game.select_cartela(
                user_id,
                cartela_value,
                user_name,
            )
        )

        return api_game_response(
            game,
            user_id,
            success=success,
            message=message,
            status=(
                200
                if success
                else 400
            ),
        )


# ============================================================
# PROCEED
# ============================================================

async def proceed_handler(request):

    try:

        data = await request.json()

    except Exception:

        data = {}

    user_id = get_user_id(
        request
    )

    if data.get("user_id"):

        user_id = str(
            data["user_id"]
        )

    async with game_lock:

        game = get_current_game()

        success, message = (
            game.proceed(
                user_id
            )
        )

        return api_game_response(
            game,
            user_id,
            success=success,
            message=message,
            status=(
                200
                if success
                else 400
            ),
        )


# ============================================================
# LEAVE
# ============================================================

async def leave_handler(request):

    try:

        data = await request.json()

    except Exception:

        data = {}

    user_id = get_user_id(
        request
    )

    if data.get("user_id"):

        user_id = str(
            data["user_id"]
        )

    async with game_lock:

        game = get_current_game()

        game.leave(
            user_id
        )

        return api_game_response(
            game,
            user_id,
            success=True,
            message=(
                "Returned to lobby."
            ),
        )


# ============================================================
# AUTO MODE
# ============================================================

async def auto_handler(request):

    try:

        data = await request.json()

    except Exception:

        data = {}

    user_id = get_user_id(
        request
    )

    if data.get("user_id"):

        user_id = str(
            data["user_id"]
        )

    # Support:
    #
    # {
    #     "auto": true
    # }
    #
    # or:
    #
    # {
    #     "enabled": true
    # }

    enabled_value = data.get(
        "auto",
        data.get(
            "enabled",
            True,
        ),
    )

    if isinstance(
        enabled_value,
        str,
    ):

        enabled_value = (
            enabled_value.lower()
            in (
                "true",
                "1",
                "yes",
                "on",
            )
        )

    enabled = bool(
        enabled_value
    )

    async with game_lock:

        game = get_current_game()

        # If player is not registered yet,
        # create their player record.
        if user_id not in game.players:

            game.add_player(
                user_id,
                get_user_name(request),
            )

        success = game.set_auto(
            user_id,
            enabled,
        )

        if not success:

            return api_game_response(
                game,
                user_id,
                success=False,
                message=(
                    "Unable to change Auto mode."
                ),
                status=400,
            )

        return api_game_response(
            game,
            user_id,
            success=True,
            message=(
                "Auto mode ON."
                if enabled
                else
                "Auto mode OFF."
            ),
        )


# ============================================================
# MANUAL BINGO CLAIM
# ============================================================

async def claim_bingo_handler(request):

    user_id = get_user_id(
        request
    )

    async with game_lock:

        game = get_current_game()

        # ----------------------------------------------------
        # GAME MUST BE PLAYING
        # ----------------------------------------------------

        if game.phase != "playing":

            return api_game_response(
                game,
                user_id,
                success=False,
                message=(
                    "The Bingo game is not currently playing."
                ),
                status=400,
            )

        # ----------------------------------------------------
        # PLAYER MUST HAVE CARTELA
        # ----------------------------------------------------

        board = game.boards.get(
            user_id
        )

        if not board:

            return api_game_response(
                game,
                user_id,
                success=False,
                message=(
                    "You do not have a cartela."
                ),
                status=400,
            )

        # ----------------------------------------------------
        # DISQUALIFIED
        # ----------------------------------------------------

        if user_id in game.disqualified_users:

            return api_game_response(
                game,
                user_id,
                success=False,
                message=(
                    "You are already disqualified."
                ),
                status=403,
            )

        # ----------------------------------------------------
        # SERVER CHECK
        #
        # We check the actual card against actual called
        # numbers. We do not trust frontend green cells.
        # ----------------------------------------------------

        patterns = (
            winning_patterns_for_board(
                board,
                game.called_numbers,
            )
        )

        # ----------------------------------------------------
        # CORRECT BINGO
        # ----------------------------------------------------

        if patterns:

            game.set_winner(
                user_id,
                patterns,
            )

            return api_game_response(
                game,
                user_id,
                success=True,
                message=(
                    "BINGO! You won!"
                ),
            )

        # ----------------------------------------------------
        # WRONG BINGO
        # ----------------------------------------------------

        game.disqualified_users.add(
            user_id
        )

        game.players.pop(
            user_id,
            None,
        )

        game.boards.pop(
            user_id,
            None,
        )

        game.proceeded_users.discard(
            user_id
        )

        return api_game_response(
            game,
            user_id,
            success=False,
            message=(
                "Wrong BINGO! "
                "You have been disqualified from this round."
            ),
            status=403,
        )


# ============================================================
# CLAIM ALIAS
# ============================================================

async def claim_handler(request):

    return await claim_bingo_handler(
        request
    )


# ============================================================
# TELEGRAM MENU
# ============================================================

async def configure_telegram_menu():

    commands = [

        BotCommand(
            command="start",
            description="Start Rodas Friend Zone",
        ),

        BotCommand(
            command="play",
            description="Play Bingo",
        ),

        BotCommand(
            command="deposit",
            description="Deposit",
        ),

        BotCommand(
            command="balance",
            description="Check balance",
        ),

        BotCommand(
            command="withdraw",
            description="Withdraw",
        ),

        BotCommand(
            command="transfer",
            description="Transfer",
        ),

        BotCommand(
            command="instruction",
            description="How to play",
        ),

        BotCommand(
            command="invite",
            description="Invite friends",
        ),

        BotCommand(
            command="support",
            description="Support",
        ),
    ]

    await bot.set_my_commands(
        commands=commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    await bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )

    logger.info(
        "Telegram private-chat menu configured."
    )


# ============================================================
# OPEN BINGO BUTTON
# ============================================================

def open_bingo_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎯 PLAY BINGO",
                    web_app=WebAppInfo(
                        url=RENDER_EXTERNAL_URL
                    ),
                )
            ]
        ]
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

@dp.message(Command("start"))
async def start_command(
    message: Message
):

    name = (
        message.from_user.first_name
        if message.from_user
        else "Player"
    )

    await message.answer(
        f"👋 Welcome, {name}!\n\n"
        "🎯 <b>Rodas Friend Zone Bingo</b>\n\n"
        "Choose an option from the menu below "
        "or open Bingo to play.",
        reply_markup=open_bingo_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("play"))
async def play_command(
    message: Message
):

    await message.answer(
        "🎯 <b>Rodas Friend Zone Bingo</b>\n\n"
        "Tap the button below to open the Bingo game.",
        reply_markup=open_bingo_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("bingo"))
async def bingo_command(
    message: Message
):

    await message.answer(
        "🎯 <b>Bingo is ready!</b>\n\n"
        "Tap below to enter the game.",
        reply_markup=open_bingo_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("deposit"))
async def deposit_command(
    message: Message
):

    await message.answer(
        "💰 <b>Deposit</b>\n\n"
        "Deposit functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("balance"))
async def balance_command(
    message: Message
):

    await message.answer(
        "💳 <b>Your Balance</b>\n\n"
        "Your current balance is being prepared.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("withdraw"))
async def withdraw_command(
    message: Message
):

    await message.answer(
        "💸 <b>Withdraw</b>\n\n"
        "Withdrawal functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("transfer"))
async def transfer_command(
    message: Message
):

    await message.answer(
        "🔄 <b>Transfer</b>\n\n"
        "Transfer functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("instruction"))
async def instruction_command(
    message: Message
):

    await message.answer(
        "📖 <b>How to Play</b>\n\n"
        "1️⃣ Choose one cartela from 1–100.\n"
        "2️⃣ Touch the same cartela again to remove it.\n"
        "3️⃣ Only one cartela can belong to one player.\n"
        "4️⃣ The 30-second countdown starts when the first player selects a cartela.\n"
        "5️⃣ When the countdown finishes, Bingo starts.\n"
        "6️⃣ Numbers are called from 1–75.\n"
        "7️⃣ Auto mode marks/checks your card automatically.\n"
        "8️⃣ With Auto OFF, mark your card manually.\n"
        "9️⃣ Complete a winning pattern and press BINGO WIN.\n\n"
        "🏆 Winning patterns:\n"
        "• Horizontal line\n"
        "• Vertical line\n"
        "• Diagonal\n"
        "• Four corners\n"
        "• Center four\n"
        "• T corners\n"
        "• Center T",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("invite"))
async def invite_command(
    message: Message
):

    await message.answer(
        "👥 <b>Invite Friends</b>\n\n"
        "Invite your friends to Rodas Friend Zone "
        "and play Bingo together.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("support"))
async def support_command(
    message: Message
):

    await message.answer(
        "🆘 <b>Support</b>\n\n"
        "For support, please contact the Rodas Friend Zone administrator.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

async def telegram_webhook(request):

    received_secret = (
        request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )
    )

    if WEBHOOK_SECRET:

        if received_secret != WEBHOOK_SECRET:

            logger.warning(
                "Rejected webhook request with invalid secret."
            )

            return web.json_response(
                {
                    "ok": False,
                    "error": "Unauthorized",
                },
                status=403,
            )

    try:

        data = await request.json()

    except Exception:

        logger.exception(
            "Telegram webhook received invalid JSON."
        )

        return web.json_response(
            {
                "ok": False,
                "error": "Invalid JSON",
            },
            status=400,
        )

    try:

        update = Update.model_validate(
            data
        )

        await dp.feed_update(
            bot,
            update,
        )

        return web.json_response(
            {
                "ok": True,
            }
        )

    except Exception:

        logger.exception(
            "Error while processing Telegram webhook update."
        )

        return web.json_response(
            {
                "ok": False,
            },
            status=500,
        )


# ============================================================
# WEBHOOK SETUP
# ============================================================

async def setup_webhook():

    logger.info(
        "Setting Telegram webhook to %s",
        WEBHOOK_URL,
    )

    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
    )

    info = await bot.get_webhook_info()

    logger.info(
        "Telegram webhook status: url=%s pending=%s last_error=%s",
        info.url,
        info.pending_update_count,
        info.last_error_message,
    )

    if info.url != WEBHOOK_URL:

        raise RuntimeError(
            "Telegram webhook verification failed. "
            f"Expected {WEBHOOK_URL}, got {info.url!r}"
        )

    logger.info(
        "Telegram webhook verified successfully."
    )


# ============================================================
# STARTUP
# ============================================================

async def on_startup(app):

    global game_loop_task

    logger.info(
        "Rodas Friend Zone Bingo starting..."
    )

    await configure_telegram_menu()

    await setup_webhook()

    game = get_current_game()

    logger.info(
        "Current Bingo game: %s",
        game.game_number,
    )

    game_loop_task = asyncio.create_task(
        game_loop()
    )

    logger.info(
        "Rodas Friend Zone Bingo is ready."
    )


# ============================================================
# CLEANUP
# ============================================================

async def on_cleanup(app):

    global game_loop_task

    logger.info(
        "Rodas Friend Zone Bingo shutting down..."
    )

    if game_loop_task:

        game_loop_task.cancel()

        try:

            await game_loop_task

        except asyncio.CancelledError:

            pass

        game_loop_task = None

    logger.info(
        "Telegram webhook kept configured."
    )

    await bot.session.close()

    logger.info(
        "Rodas Friend Zone Bingo shutdown complete."
    )


# ============================================================
# CREATE APP
# ============================================================

def create_app():

    application = web.Application()

    # --------------------------------------------------------
    # MAIN WEB APP
    # --------------------------------------------------------

    application.router.add_get(
        "/",
        index_handler,
    )

    # --------------------------------------------------------
    # HEALTH
    # --------------------------------------------------------

    application.router.add_get(
        "/health",
        health_handler,
    )

    # --------------------------------------------------------
    # GAME STATE
    # --------------------------------------------------------

    application.router.add_get(
        "/api/state",
        state_handler,
    )

    application.router.add_get(
        "/api/refresh",
        refresh_handler,
    )

    # --------------------------------------------------------
    # CARTELA
    # --------------------------------------------------------

    application.router.add_post(
        "/api/select",
        select_handler,
    )

    # --------------------------------------------------------
    # PROCEED
    # --------------------------------------------------------

    application.router.add_post(
        "/api/proceed",
        proceed_handler,
    )

    # --------------------------------------------------------
    # LEAVE
    # --------------------------------------------------------

    application.router.add_post(
        "/api/leave",
        leave_handler,
    )

    # --------------------------------------------------------
    # AUTO
    # --------------------------------------------------------

    application.router.add_post(
        "/api/auto",
        auto_handler,
    )

    # --------------------------------------------------------
    # BINGO CLAIM
    # --------------------------------------------------------

    application.router.add_post(
        "/api/claim-bingo",
        claim_bingo_handler,
    )

    application.router.add_post(
        "/api/claim",
        claim_handler,
    )

    # --------------------------------------------------------
    # TELEGRAM WEBHOOK
    # --------------------------------------------------------

    application.router.add_post(
        WEBHOOK_PATH,
        telegram_webhook,
    )

    # --------------------------------------------------------
    # START / CLEANUP
    # --------------------------------------------------------

    application.on_startup.append(
        on_startup
    )

    application.on_cleanup.append(
        on_cleanup
    )

    return application


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Starting Rodas Friend Zone Bingo on port %s",
        PORT,
    )

    app = create_app()

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT,
    )
