###
# Copyright (c) 2012, DAn
# All rights reserved.
#
#
###

import supybot.utils as utils
import supybot.ircdb as ircdb
from supybot.commands import *
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks

import os
import traceback
import datetime
import supybot.ircmsgs as ircmsgs
import supybot.schedule as schedule

try:
    import sqlite3
except ImportError:
    from pysqlite2 import dbapi2 as sqlite3 # for python2.4

class Polls(callbacks.Plugin, plugins.ChannelDBHandler):
    """Poll for in channel
    Make polls and people can vote on them"""

    def __init__(self, irc):
        callbacks.Plugin.__init__(self, irc)
        plugins.ChannelDBHandler.__init__(self)
        self.poll_schedules = []
    
    def makeDb(self, filename):
        if os.path.exists(filename):
            db = sqlite3.connect(filename, detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)
            db.text_factory = str
            return db
        db = sqlite3.connect(filename, detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)
        db.text_factory = str
        cursor = db.cursor()
        cursor.execute("""CREATE TABLE polls(
                    id INTEGER PRIMARY KEY,
                    started_time TIMESTAMP,
                    active INTEGER default 1 ,
                    closed TIMESTAMP,
                    question TEXT)""")
        cursor.execute("""CREATE TABLE choices(
                    poll_id INTEGER,
                    choice_num INTEGER,
                    choice TEXT)""")
        cursor.execute("""CREATE TABLE votes(
                    id INTEGER PRIMARY KEY,
                    poll_id INTEGER,
                    voter_nick TEXT,
                    voter_host TEXT,
                    choice INTEGER,
                    time timestamp)""")
        db.commit()
        return db

    def _runPoll(self, irc, channel, pollid):
        db = self.getDb(channel)
        cursor = db.cursor()
        cursor.execute('SELECT active,closed,question FROM polls WHERE id=?', (pollid,))
        active, closed, question = cursor.fetchone()

        if not active or closed:
            try:
                schedule.removeEvent('%s_poll_%s' % (channel, pollid))
                self.poll_schedules.remove('%s_poll_%s' % (channel, pollid))
            except:
                pass
            return

        irc.sendMsg(ircmsgs.privmsg(channel, 'Poll #%s: %s' % (pollid, question)))

        cursor.execute('SELECT choice_num,choice FROM choices WHERE poll_id=? ORDER BY choice_num', (pollid,))

        choice_row = cursor.fetchone()
        while choice_row is not None:
            irc.sendMsg(ircmsgs.privmsg(channel, ('%s: %s' % (choice_row[0], choice_row[1]))))
            choice_row = cursor.fetchone()
        
        irc.sendMsg(ircmsgs.privmsg(channel, 'To vote, do !vote %s <choice number>' % pollid)) 


    def newpoll(self, irc, msg, args, channel, interval, answers, question):
        """<number of minutes for announce interval> <"answer, answer, ..."> question
        add a new poll"""
        capability = ircdb.makeChannelCapability(channel, 'op')
        #if not (irc.state.channels[channel].isOp(msg.nick) or
        #        ircdb.checkCapability(msg.prefix, capability)):
        #    irc.error('Need channel ops to add poll')
        #    return
        if not ircdb.checkCapability(msg.prefix, capability):
            irc.error('Need ops')
            return

        db = self.getDb(channel)
        cursor = db.cursor()
        cursor.execute('INSERT INTO polls VALUES (?,?,?,?,?)', (None, datetime.datetime.now(), 1, None, question))
        pollid = cursor.lastrowid

        def genAnswers():
            for i, answer in enumerate(answers, start=1):
                yield pollid, i, answer

        cursor.executemany('INSERT INTO choices VALUES (?,?,?)', genAnswers())

        db.commit()

        irc.reply('Started new poll #%s' % pollid)

        def runPoll():
            self._runPoll(irc, channel, pollid)

        schedule.addPeriodicEvent(runPoll, interval*60, name='%s_poll_%s' % (channel, pollid))
        self.poll_schedules.append('%s_poll_%s' % (channel, pollid))
        irc.replySuccess()

    newpoll = wrap(newpoll, ['channeldb', 'Op', 'positiveInt', commalist('something'), 'text'])

    def vote(self, irc, msg, args, channel, pollid, choice):
        """<poll id number> <choice number>
        votes on poll"""
        db = self.getDb(channel)
        cursor = db.cursor()

        cursor.execute('SELECT closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()
        if result is None:
            irc.error('No poll with that id')
            return
        if result[0] is not None:
            irc.error('This poll was closed on %s' % result[0].strftime('%Y-%m-%d at %-I:%M %p'))
            return

        cursor.execute('SELECT * FROM choices WHERE poll_id=? AND choice_num=?', (pollid, choice))
        result = cursor.fetchone()
        if result is None:
            irc.error('That is not a choice for that poll')
            return
        
        cursor.execute('SELECT choice,time FROM votes WHERE (voter_nick=? OR voter_host=?) AND poll_id=?', (msg.nick, msg.host, pollid))
        result = cursor.fetchone()

        if result is not None:
            irc.error('You have already voted for %s on %s' % (result[0], result[1].strftime('%Y-%m-%d at %-I:%M %p')))
            return

        cursor.execute('INSERT INTO votes VALUES (?,?,?,?,?,?)', (None, pollid, msg.nick, msg.host, choice, datetime.datetime.now()))
        db.commit()

        irc.sendMsg(ircmsgs.privmsg(channel, 'Your vote on poll #%s for %s has been inputed, sending you results in PM' % (pollid, choice)))

        irc.sendMsg(ircmsgs.privmsg(msg.nick, 'Here is results for poll #%s, you just voted for %s' % (pollid, choice)))
        cursor2 = db.cursor()
        cursor.execute('SELECT choice_num,choice FROM choices WHERE poll_id=? ORDER BY choice_num', (pollid,))
        choice_row = cursor.fetchone()
        while choice_row is not None:
            cursor2.execute('SELECT count(*) FROM votes WHERE poll_id=? AND choice=?', (pollid, choice_row[0],))
            vote_row = cursor2.fetchone()
            irc.sendMsg(ircmsgs.privmsg(msg.nick, '%s: %s - %s votes' % (choice_row[0], choice_row[1], vote_row[0])))
            choice_row = cursor.fetchone()

    vote = wrap(vote, ['channeldb', 'positiveInt', 'positiveInt'])

    def results(self, irc, msg, args, channel, pollid):
        """[channel] <pollid>
        PM you results of poll. You have to had voted already"""
        db = self.getDb(channel)
        cursor = db.cursor()

        cursor.execute('SELECT id FROM votes WHERE poll_id=? AND (voter_nick=? OR voter_host=?)', (pollid, msg.nick, msg.host))
        result = cursor.fetchone()
        if result is None:
            irc.error('You need to vote first to view results!')
            return

        cursor.execute('SELECT choice_num,choice FROM choices WHERE poll_id=? ORDER BY choice_num', (pollid,))
        choice_row = cursor.fetchone()
        
        if choice_row is None:
            irc.error('I dont think that poll id exists')
            return

        irc.sendMsg(ircmsgs.privmsg(msg.nick, 'Here is results for poll #%s' % pollid))
        cursor2 = db.cursor()
        while choice_row is not None:
            cursor2.execute('SELECT count(*) FROM votes WHERE poll_id=? AND choice=?', (pollid, choice_row[0],))
            vote_row = cursor2.fetchone()
            irc.sendMsg(ircmsgs.privmsg(msg.nick, ('%s: %s - %s votes' % (choice_row[0], choice_row[1], vote_row[0]))))
            choice_row = cursor.fetchone()

        irc.replySuccess()

    results = wrap(results, ['channeldb', 'positiveInt'])

    def openpolls(self, irc, msg, args):
        """takes no arguments
        PMs you list of all polls"""
        db = self.getDb(channel)
        cursor = db.cursor()

        cursor.execute('SELECT id,question FROM polls WHERE closed=NULL')
        row = cursor.fetchone()

        cursor2 = db.cursor()
        while row is not None:
            cursor2.execute('SELECT choice_num,choice FROM choices WHERE poll_id=? ORDER BY choice_num', (row[0],))
            row = cursor.fetchone()

    openpolls = wrap(openpolls)


    def pollon(self, irc, msg, args, channel, pollid, interval):
        """[channel] <pollid> <interval in minutes>
        Turn a poll on active so it is announcing, with interval"""
        db = self.getDb(channel)
        cursor = db.cursor()

        cursor.execute('SELECT active,closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()
        if result is None:
            irc.error('That poll id does not exist')
            return

        if result[0] == 1:
            irc.error('Poll is already active')
            return
        
        db.execute('UPDATE polls SET active=? WHERE id=?', (1, pollid))
        db.commit()

        if result[1] is not None:
            irc.reply('Note: you are turning on closed poll. I will not start announcing it')
            return

        def runPoll():
            self._runPoll(irc, channel, pollid)

        schedule.addPeriodicEvent(runPoll, interval*60, name='%s_poll_%s' % (channel, pollid))
        self.poll_schedules.append('%s_poll_%s' % (channel, pollid))

        irc.replySuccess()

    pollon = wrap(pollon, ['channeldb', 'Op', 'positiveInt', 'positiveInt'])

    def polloff(self, irc, msg, args, channel, pollid):
        """[channel] <pollid>
        Stop a poll from announcing"""
        db = self.getDb(channel)
        cursor = db.cursor()

        cursor.execute('SELECT active,closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()

        if result is None:
            irc.error('That poll id does not exist')
            return

        if result[0] == 0:
            irc.error('Poll is already off')
            return

        if result[1] is not None:
            irc.reply('Note: you are turning off a closed poll')

        cursor.execute('UPDATE polls SET active=? WHERE id=?', (0, pollid))
        db.commit()

        try:
            schedule.removeEvent('%s_poll_%s' % (channel, pollid))
            self.poll_schedules.remove('%s_poll_%s' % (channel, pollid))
        except:
            pass

        irc.replySuccess()

    polloff = wrap(polloff, ['channeldb', 'Op', 'positiveInt'])

    def closepoll(self, irc, msg, args, channel, pollid):
        """[channel] <pollid>
        Close poll. No more voting."""
        db = self.getDb(channel)
        cursor = db.cursor()

        cursor.execute('SELECT active,closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()

        if result is None:
            irc.error('Poll id doesnt exist')
            return

        if result[1] is not None:
            irc.error('Poll already closed on %s' % result[1].strftime('%Y-%m-%d at %-I:%M %p'))
            return

        cursor.execute('UPDATE polls SET closed=? WHERE id=?', (datetime.datetime.now(), pollid))
        db.commit()

        try:
            schedule.removeEvent('%s_poll_%s' % (channel, pollid))
            self.poll_schedules.remove('%s_poll_%s' % (channel, pollid))
        except:
            pass

        irc.replySuccess()

    closepoll = wrap(closepoll, ['channeldb', 'Op', 'positiveInt'])

    def openpoll(self, irc, msg, args, channel, pollid, interval):
        """[channel] <pollid>
        Open poll for voting. Starts announcing if set to active"""
        db = self.getDb(channel)
        cursor = db.cursor()

        cursor.execute('SELECT active,closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()

        if result is None:
            irc.error('Poll id doesnt exist')
            return

        if result[1] is None:
            irc.error('Poll is still open')
            return

        cursor.execute('UPDATE polls SET closed=? WHERE id=?', (None, pollid))
        db.commit()

        if result[0] == 1:
            if interval is None:
                irc.reply('Note: Poll set to active, but you didnt supply interval, using default of 10 minutes')
                interval = 10
            def runPoll():
                self._runPoll(irc, channel, pollid)

            schedule.addPeriodicEvent(runPoll, interval*60, name='%s_poll_%s' % (channel, pollid))
            self.poll_schedules.append('%s_poll_%s' % (channel, pollid))

    openpoll = wrap(openpoll, ['channeldb', 'Op', 'positiveInt', additional('positiveInt')])

    def die(self):
        for schedule_name in self.poll_schedules:
            schedule.removeEvent(schedule_name)

Class = Polls


# vim:set shiftwidth=4 softtabstop=4 expandtab:
