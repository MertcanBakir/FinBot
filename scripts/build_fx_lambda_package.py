"""Build the fx Lambda deployment package."""

if __package__:
    from .build_lambda_package import main
else:
    from build_lambda_package import main


if __name__ == "__main__":
    main("fx")
