# The official Bookworm image includes Git. The manifest pins its OS packages
# and Python interpreter for every supported architecture.
FROM python:3.11-bookworm@sha256:726d304906fa5eb468e5531e366548d1903286352f5adb54ef508ac01fbd80c6
COPY requirements/dev.txt /tmp/cirra-requirements.txt
RUN git --version && python -m pip install --no-cache-dir --require-hashes \
    --only-binary=:all: -r /tmp/cirra-requirements.txt
USER 65534:65534
