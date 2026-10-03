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

# Player pays 10 for each round.
BET_AMOUNT = 10

# Cartela selection time.
SELECTION_SECONDS = 30

# Short countdown before numbers start.
STARTING_SECONDS = 3

# Time between called numbers.
DRAW_INTERVAL = 3

# Winner/result screen duration.
RESULT_SECONDS = 30

# Winner receives 75% of total bets for now.
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
# HELPERS
# ============================================================

def now():
    return time.time()


def get_user_id(request):
    """
    Gets the Telegram user ID from the web app.

    The frontend should send:
        X-User-ID

    We also support:
        X-Telegram-User-ID

    IMPORTANT:
    We do not use "guest" for real players if the
    Telegram WebApp provides an ID.
    """

    value = (
        request.headers.get("X-User-ID")
        or request.headers.get("X-Telegram-User-ID")
        or request.query.get("user_id")
        or "guest"
    )

    return str(value)


def get_user_name(request):
    """
    Gets the player's name from the web app.
    """

    return (
        request.headers.get("X-User-Name")
        or request.query.get("user_name")
        or "Player"
    )


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def generate_game_number():
    global next_game_number

    number = next_game_number

    next_game_number += 1

    return number


# ============================================================
# CARTELA GENERATION
# ============================================================

def bingo_column(column):
    ranges = {
        "B": range(1, 16),
        "I": range(16, 31),
        "N": range(31, 46),
        "G": range(46, 61),
        "O": range(61, 76),
    }

    values = list(ranges[column])

    return random.sample(values, 5)


def generate_cartela(cartela_number=None):
    """
    Creates a Bingo card.

    Cartela 35 has the exact card requested by the user.
    Other cartelas receive their own deterministic card
    based on their cartela number.

    This means the same cartela number keeps the same
    card during the application lifetime.
    """

    # --------------------------------------------------------
    # EXACT CARTELA 35 REQUESTED BY USER
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
    # DETERMINISTIC CARDS FOR OTHER CARTELAS
    # --------------------------------------------------------

    rng = random.Random(
        50000 + safe_int(
            cartela_number,
            1,
        )
    )

    columns = [
        "B",
        "I",
        "N",
        "G",
        "O",
    ]

    ranges = {
        "B": list(range(1, 16)),
        "I": list(range(16, 31)),
        "N": list(range(31, 46)),
        "G": list(range(46, 61)),
        "O": list(range(61, 76)),
    }

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
# PATTERN HELPERS
# ============================================================

def cell_is_complete(
    value,
    called,
):
    return (
        value == "FREE"
        or value in called
    )


def pattern_cells(
    board,
    pattern_name,
):
    """
    Returns the board positions belonging to a pattern.
    """

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
    # DIAGONAL
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
    #
    # Top row + left/right top corner sides.
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
    #
    # Center column + center row.
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


def winning_patterns_for_board(
    board,
    called_numbers,
):
    """
    Checks every winning pattern requested by the user.
    """

    called = set(
        called_numbers
    )

    patterns = []

    # --------------------------------------------------------
    # HORIZONTAL LINES
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
    # VERTICAL LINES
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
    # DIAGONALS
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


def check_pattern(
    board,
    called_numbers,
):
    return bool(
        winning_patterns_for_board(
            board,
            called_numbers,
        )
    )


def pattern_display_name(pattern):
    names = {
        "four-corners":
            "Four Corners",

        "center-four":
            "Center Four",

        "t-corners":
            "T Corners",

        "center-t":
            "Center T",

        "diagonal-main":
            "Diagonal",

        "diagonal-other":
            "Diagonal",

    }

    if pattern in names:

        return names[pattern]

    if pattern.startswith(
        "horizontal-"
    ):

        return "Horizontal Line"

    if pattern.startswith(
        "vertical-"
    ):

        return "Vertical Line"

    return pattern.replace(
        "-",
        " ",
    ).title()


# ============================================================
# BINGO GAME
# ============================================================

class BingoGame:

    def __init__(
        self,
        game_number,
    ):

        self.game_number = (
            game_number
        )

        # ----------------------------------------------------
        # PHASE
        # ----------------------------------------------------

        self.phase = "selection"

        self.selection_started_at = (
            now()
        )

        self.selection_seconds = (
            SELECTION_SECONDS
        )

        self.starting_started_at = None

        self.starting_seconds = (
            STARTING_SECONDS
        )

        # ----------------------------------------------------
        # PLAYERS
        # ----------------------------------------------------

        self.players = {}

        self.player_names = {}

        self.boards = {}

        self.left_users = set()

        self.disqualified_users = set()

        self.proceeded_users = set()

        # ----------------------------------------------------
        # CALLED NUMBERS
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
        # MONEY
        # ----------------------------------------------------

        self.total_bets = 0

        self.prize = 0

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        self.result_started_at = None

    # ========================================================
    # TIMERS
    # ========================================================

    def selection_remaining(self):

        elapsed = (
            now()
            - self.selection_started_at
        )

        return max(
            0,
            int(
                self.selection_seconds
                - elapsed
            ),
        )

    def starting_remaining(self):

        if not self.starting_started_at:

            return self.starting_seconds

        elapsed = (
            now()
            - self.starting_started_at
        )

        return max(
            0,
            int(
                self.starting_seconds
                - elapsed
            ),
        )

    def result_remaining(self):

        if not self.result_started_at:

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

    def reset_selection_timer(self):

        self.selection_started_at = (
            now()
        )

    # ========================================================
    # START COUNTDOWN
    # ========================================================

    def start_countdown(self):

        self.phase = "starting"

        self.starting_started_at = (
            now()
        )

        logger.info(
            "Game %s: starting countdown.",
            self.game_number,
        )

    # ========================================================
    # START PLAYING
    # ========================================================

    def start_playing(self):

        self.phase = "playing"

        self.started_at = (
            now()
        )

        self.last_draw_at = (
            now()
        )

        self.starting_started_at = None

        logger.info(
            "Game %s started with %s player(s).",
            self.game_number,
            len(self.players),
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

            self.left_users.remove(
                user_id
            )

        if user_id in self.disqualified_users:

            return False

        if user_id not in self.players:

            self.players[user_id] = None

            self.total_bets += (
                BET_AMOUNT
            )

        if name:

            self.player_names[
                user_id
            ] = name

        return True

    # ========================================================
    # SELECT CARTELA
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

        if not (
            CARTELA_MIN
            <= cartela_number
            <= CARTELA_MAX
        ):

            return (
                False,
                "Invalid cartela number.",
            )

        if user_id in (
            self.disqualified_users
        ):

            return (
                False,
                "You are disqualified from this round.",
            )

        added = self.add_player(
            user_id,
            name,
        )

        if not added:

            return (
                False,
                "You cannot join this round.",
            )

        # ----------------------------------------------------
        # CHECK CARTELA ALREADY TAKEN
        # ----------------------------------------------------

        for (
            other_user_id,
            selected
        ) in self.players.items():

            if (
                other_user_id != user_id
                and selected == cartela_number
            ):

                return (
                    False,
                    "That cartela is already selected.",
                )

        # ----------------------------------------------------
        # SAVE PLAYER
        # ----------------------------------------------------

        self.players[user_id] = (
            cartela_number
        )

        # Generate the card based on cartela number.
        self.boards[user_id] = (
            generate_cartela(
                cartela_number
            )
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

        return True

    # ========================================================
    # PROCEED
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

        self.start_countdown()

        return (
            True,
            "Proceeding to the game.",
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

        self.proceeded_users.discard(
            user_id
        )

        return True

    # ========================================================
    # DISQUALIFY
    # ========================================================

    def disqualify(
        self,
        user_id,
    ):

        user_id = str(
            user_id
        )

        self.disqualified_users.add(
            user_id
        )

        self.players.pop(
            user_id,
            None,
        )

        self.boards.pop(
            user_id,
            None,
        )

        self.proceeded_users.discard(
            user_id
        )

        logger.warning(
            "Game %s: user %s disqualified.",
            self.game_number,
            user_id,
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

        self.last_draw_at = (
            now()
        )

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

        self.winner_user_id = (
            user_id
        )

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

        self.winner_patterns = (
            patterns
        )

        self.winner_pattern = (
            pattern_display_name(
                patterns[0]
            )
        )

        # Save a copy for the result screen.
        self.winner_board = [
            row.copy()
            for row in board
        ]

        # Calculate prize.
        self.prize = round(
            self.total_bets
            * PRIZE_PERCENT,
            2,
        )

        self.phase = "result"

        self.result_started_at = (
            now()
        )

        logger.info(
            "Game %s: WINNER = %s | pattern = %s | prize = %s",
            self.game_number,
            self.winner,
            self.winner_pattern,
            self.prize,
        )

        return True

    # ========================================================
    # AUTOMATIC WIN CHECK
    # ========================================================

    def check_automatic_winner(self):

        if self.phase != "playing":

            return False

        for (
            user_id,
            board
        ) in list(
            self.boards.items()
        ):

            if user_id in (
                self.disqualified_users
            ):

                continue

            if user_id not in self.players:

                continue

            if self.players.get(
                user_id
            ) is None:

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
    # SERIALIZE
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

        taken_cartelas = [
            selected
            for selected in self.players.values()
            if selected is not None
        ]

        proceeded = False

        if user_id:

            proceeded = (
                user_id
                in self.proceeded_users
            )

        # ----------------------------------------------------
        # WINNER CARD
        # ----------------------------------------------------

        winner_board = (
            self.winner_board
        )

        # ----------------------------------------------------
        # WINNING CELLS
        # ----------------------------------------------------

        winning_cells = []

        if (
            winner_board
            and self.winner_patterns
        ):

            for pattern in (
                self.winner_patterns
            ):

                winning_cells.extend(
                    pattern_cells(
                        winner_board,
                        pattern,
                    )
                )

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

            # TIMERS
            "selection_remaining":
                (
                    self.selection_remaining()
                    if self.phase
                    == "selection"
                    else 0
                ),

            "start_remaining":
                (
                    self.starting_remaining()
                    if self.phase
                    == "starting"
                    else 0
                ),

            "result_remaining":
                (
                    self.result_remaining()
                    if self.phase
                    == "result"
                    else 0
                ),

            # PLAYERS
            "players":
                len(self.players),

            "player_count":
                len(self.players),

            # CARTELA
            "my_cartela":
                my_cartela,

            "selected":
                my_cartela,

            "proceeded":
                proceeded,

            "taken_cartelas":
                taken_cartelas,

            "taken":
                taken_cartelas,

            # CARD
            "my_board":
                my_board,

            "card":
                my_board,

            # CALLED NUMBERS
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

            # WINNER
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
                winner_board,

            "winning_cells":
                winning_cells,

            # MONEY
            "wallet":
                0,

            "balance":
                0,

            "bet":
                BET_AMOUNT,

            "total_bets":
                self.total_bets,

            "prize":
                self.prize,

            "derash":
                self.prize,

            # PLAYER
            "left":
                (
                    user_id
                    in self.left_users
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

    game_number = (
        generate_game_number()
    )

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
                # SELECTION
                # =================================================

                if game.phase == "selection":

                    if (
                        game.selection_remaining()
                        <= 0
                    ):

                        # Only start if there is at least
                        # one selected player.
                        selected_players = [
                            user_id
                            for user_id, selected
                            in game.players.items()
                            if selected is not None
                        ]

                        if selected_players:

                            game.start_countdown()

                            logger.info(
                                "Game %s selection finished.",
                                game.game_number,
                            )

                        else:

                            game.reset_selection_timer()

                # =================================================
                # STARTING
                # =================================================

                elif game.phase == "starting":

                    if (
                        game.starting_remaining()
                        <= 0
                    ):

                        selected_players = [
                            user_id
                            for user_id, selected
                            in game.players.items()
                            if selected is not None
                        ]

                        if selected_players:

                            game.start_playing()

                        else:

                            game.phase = (
                                "selection"
                            )

                            game.reset_selection_timer()

                # =================================================
                # PLAYING
                # =================================================

                elif game.phase == "playing":

                    if (
                        game.last_draw_at
                        is None
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

                            # ------------------------------------------------
                            # AUTO MODE IS SERVER SIDE.
                            #
                            # We automatically check every player's
                            # card after EVERY number.
                            # ------------------------------------------------

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

                        game.phase = (
                            "finished"
                        )

                        game.finished_at = (
                            now()
                        )

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


async def state_handler(request):

    user_id = get_user_id(
        request
    )

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

    payload["success"] = (
        success
    )

    payload["ok"] = (
        success
    )

    if message is not None:

        payload["message"] = (
            message
        )

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
        # DESELECT
        # ----------------------------------------------------

        if (
            cartela_value is None
            or cartela_value == ""
        ):

            game.add_player(
                user_id,
                user_name,
            )

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
        # SELECT
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

    if data.get(
        "user_id"
    ):

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

    if data.get(
        "user_id"
    ):

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
                "Returned to cartela selection."
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
                    "The game is not currently playing."
                ),
                status=400,
            )

        # ----------------------------------------------------
        # PLAYER MUST EXIST
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
        # ALREADY DISQUALIFIED
        # ----------------------------------------------------

        if user_id in (
            game.disqualified_users
        ):

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
        # We DO NOT trust the player's green cells.
        #
        # The server checks the actual card against
        # numbers that were genuinely called.
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
        #
        # Remove player from this round.
        # ----------------------------------------------------

        game.disqualify(
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

@dp.message(
    Command("start")
)
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


@dp.message(
    Command("play")
)
async def play_command(
    message: Message
):

    await message.answer(
        "🎯 <b>Rodas Friend Zone Bingo</b>\n\n"
        "Tap the button below to open the Bingo game.",
        reply_markup=open_bingo_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(
    Command("bingo")
)
async def bingo_command(
    message: Message
):

    await message.answer(
        "🎯 <b>Bingo is ready!</b>\n\n"
        "Tap below to enter the game.",
        reply_markup=open_bingo_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@dp.message(
    Command("deposit")
)
async def deposit_command(
    message: Message
):

    await message.answer(
        "💰 <b>Deposit</b>\n\n"
        "Deposit functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(
    Command("balance")
)
async def balance_command(
    message: Message
):

    await message.answer(
        "💳 <b>Your Balance</b>\n\n"
        "Your current balance is being prepared.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(
    Command("withdraw")
)
async def withdraw_command(
    message: Message
):

    await message.answer(
        "💸 <b>Withdraw</b>\n\n"
        "Withdrawal functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(
    Command("transfer")
)
async def transfer_command(
    message: Message
):

    await message.answer(
        "🔄 <b>Transfer</b>\n\n"
        "Transfer functionality will be available here.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(
    Command("instruction")
)
async def instruction_command(
    message: Message
):

    await message.answer(
        "📖 <b>How to Play</b>\n\n"
        "1️⃣ Choose a cartela.\n"
        "2️⃣ Press PROCEED.\n"
        "3️⃣ Numbers will be called.\n"
        "4️⃣ Auto mode marks your numbers automatically.\n"
        "5️⃣ With Auto OFF, mark called numbers yourself.\n"
        "6️⃣ Complete a winning pattern.\n"
        "7️⃣ With Auto OFF, press BINGO WIN.\n\n"
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


@dp.message(
    Command("invite")
)
async def invite_command(
    message: Message
):

    await message.answer(
        "👥 <b>Invite Friends</b>\n\n"
        "Invite your friends to Rodas Friend Zone "
        "and play Bingo together.",
        parse_mode=ParseMode.HTML,
    )


@dp.message(
    Command("support")
)
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

    # Keep webhook configured.
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

    application.router.add_get(
        "/",
        index_handler,
    )

    application.router.add_get(
        "/health",
        health_handler,
    )

    application.router.add_get(
        "/api/state",
        state_handler,
    )

    application.router.add_post(
        "/api/select",
        select_handler,
    )

    application.router.add_post(
        "/api/proceed",
        proceed_handler,
    )

    application.router.add_post(
        "/api/leave",
        leave_handler,
    )

    application.router.add_post(
        "/api/claim-bingo",
        claim_bingo_handler,
    )

    application.router.add_post(
        "/api/claim",
        claim_handler,
    )

    application.router.add_post(
        WEBHOOK_PATH,
        telegram_webhook,
    )

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
