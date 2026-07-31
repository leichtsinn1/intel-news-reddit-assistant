# Intel News Reddit Assistant

A private, non commercial Intel news monitoring and Reddit duplicate prevention tool.

## Purpose

The application collects publicly available Intel related technology news from approved RSS sources. It filters potential news candidates locally and helps determine whether the same article or news event has already been submitted to Reddit.

The initial target communities are:

* r/IntelArc
* r/intelstock

The application is intended to reduce duplicate submissions, improve source quality and ensure that appropriate subreddit flairs are used.

## Workflow

1. Retrieve approved external RSS feeds approximately every two hours
2. Filter Intel related articles locally
3. Store candidate metadata in a local SQLite database
4. Check recent Reddit submissions for duplicate URLs and similar topics
5. Send suitable candidates to a private Signal approval workflow
6. Require explicit manual approval
7. Perform a second Reddit duplicate check
8. Prepare or submit the original source link with an appropriate flair

## Safety and limitations

The application will not:

* Vote on Reddit content
* Automatically comment
* Send Reddit private messages
* Profile individual Reddit users
* Collect user histories
* Train artificial intelligence models using Reddit data
* Publish posts without explicit manual approval
* Circumvent subreddit rules or moderator review

Rumors and leaks must be clearly identified as unconfirmed.

Original sources are preferred over secondary reporting.

## Data handling

Only the minimum Reddit metadata necessary for duplicate detection will be processed. This may include:

* Submission identifiers
* Titles
* URLs
* Timestamps
* Subreddit names
* Flair identifiers

Authentication credentials, Signal account data, private keys, telephone numbers and database files are not included in this repository.

## Technology

* Debian 13
* Python
* SQLite
* systemd
* signal-cli
* Reddit OAuth API

## Status

Early prototype development.

The RSS collection, local filtering, SQLite storage, scheduled execution and private Signal notifications are currently operational.

Reddit API integration will be added after access has been reviewed and approved by Reddit.
