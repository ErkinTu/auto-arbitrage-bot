from aiogram.fsm.state import State, StatesGroup


class AddFilter(StatesGroup):
    brand = State()
    model = State()
    years = State()
    mileage = State()
    price = State()
    below_avg = State()
