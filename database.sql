CREATE TABLE players (
    name         varchar(80) PRIMARY KEY,
    first_seen   bigint,
    last_seen    bigint NOT NULL,
    last_server  varchar(80),
    minutes      int NOT NULL
);

CREATE TABLE popular (
    name         varchar(80) PRIMARY KEY,
    last_popular timestamp NOT NULL
);
