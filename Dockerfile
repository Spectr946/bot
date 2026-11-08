FROM python:3.13
WORKDIR /app
COPY *.py requirements.txt /app/
COPY templates /app/templates
COPY static /app/static
RUN pip install -r requirements.txt
EXPOSE 8080
CMD ["python", "main.py"]
