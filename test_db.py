#!/usr/bin/env python3

from budget_bot import BudgetDatabase


def test_database():
    print("Testing database functionality...")

    db = BudgetDatabase("test_budget.db")

    test_user_id = 123456789

    print("Adding test transactions...")
    db.add_transaction(test_user_id, 100.50, "USD", "Базовая еда", "Test groceries")
    db.add_transaction(test_user_id, 50.0, "EUR", "Бары/рестораны", "Dinner out")
    db.add_transaction(test_user_id, 1500.0, "RSD", "Такси", None)

    print("Getting stats for last 30 days...")
    stats = db.get_stats(test_user_id)

    print("Results:")
    for category, currency, amount in stats:
        print(f"  {category}: {amount:.2f} {currency}")

    print("\nDatabase test completed successfully!")
    print("You can delete 'test_budget.db' file if you don't need it.")


if __name__ == "__main__":
    test_database()
