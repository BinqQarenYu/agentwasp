FROM nginx:alpine
COPY nginx.conf /etc/nginx/nginx.conf
COPY html/ /usr/share/nginx/html/
COPY html/docs/ /usr/share/nginx/docs/
RUN mkdir -p /usr/share/nginx/acme
