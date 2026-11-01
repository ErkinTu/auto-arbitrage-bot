from aiogram.fsm.state import State, StatesGroup


class FilterPanel(StatesGroup):
    """Панель фильтра (`bot/filter_panel.py`): одно состояние на всё время.

    Шаг, черновик и id сообщения-панели лежат в данных FSM. Пошаговые
    состояния прежнего визарда (`AddFilter`) больше не нужны: порядок шагов
    свободный, а ввод текстом разбирается по текущему шагу из данных.
    """

    active = State()
