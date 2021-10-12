FROM node:12-bullseye
ARG LOGIO_RELATIVE_PATH
RUN apt update && apt install -y build-essential python2
WORKDIR /usr/local/src/log.io/server
# in the application there is a PUBLIC_URL which is replaced by the relative part
# of the homepage section *facepalm*; the host doesnt matter
# ../ui/package.json "https://example.com$LOGIO_RELATIVE_PATH"
RUN npm install -g typescript
ADD log.io /usr/local/src/log.io
RUN ./bin/build-ui.sh && \
npm install && \
npm update && \ 
npm run build:js && \
npm set strict-ssl false && \
npm install -g log.io-file-input

ENV LOGIO_SERVER_CONFIG_PATH=/root/.log.io/server.json
ENV LOGIO_FILE_INPUT_CONFIG_PATH=/root/.log.io/file_input.json
ADD server.json "$LOGIO_SERVER_CONFIG_PATH"
ADD file_input.json "$LOGIO_FILE_INPUT_CONFIG_PATH"
ADD run.sh /usr/local/bin/run.sh
RUN chmod a+x /usr/local/bin/run.sh
CMD run.sh