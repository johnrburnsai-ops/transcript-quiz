from app import QuizApp


def main() -> None:
    application = QuizApp()
    try:
        application.mainloop()
    finally:
        if not application._closing:
            application.api.close(timeout=2.0)


if __name__ == "__main__":
    main()
