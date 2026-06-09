import os

# The CI build overwrites this file with a hardcoded constant for each environment.
# Locally, set the FOCALE_ENV environment variable to override (default: "production").
ENVIRONMENT = os.environ.get("FOCALE_ENV", "production")
