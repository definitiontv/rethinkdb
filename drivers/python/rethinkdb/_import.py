#!/usr/bin/env python

'''`rethinkdb import` loads data into a RethinkDB cluster'''

from __future__ import print_function

import codecs, collections, csv, ctypes, json, multiprocessing
import optparse, os, re, signal, sys, time, traceback

try:
    unicode
except NameError:
    unicode = str
try:
    xrange
except NameError:
    xrange = range
try:
    from Queue import Empty, Full
except ImportError:
    from queue import Empty, Full
try:
    from multiprocessing import Queue, SimpleQueue
except ImportError:
    from multiprocessing.queues import Queue, SimpleQueue

# Used because of API differences in the csv module, taken from
# http://python3porting.com/problems.html
PY3 = sys.version > "3"

#json parameters
json_read_chunk_size = 128 * 1024
json_max_buffer_size = 128 * 1024 * 1024
max_nesting_depth = 100
default_batch_size = 200

from . import utils_common
r = utils_common.r

Error = collections.namedtuple("Error", ["message", "traceback", "file"])

class SourceFile(object):
    format         = None # set by subclasses
    
    name           = None
    
    db             = None
    table          = None
    primary_key    = None
    indexes        = None
    options        = None
    
    start_time     = None
    end_time       = None
    
    _source        = None # open filehandle for the source
    
    # - internal synchronization variables
    
    _bytes_size    = None
    _bytes_read    = None # -1 until started
    
    _total_rows    = None # -1 until known
    _rows_read     = None
    _rows_written  = None
    
    def __init__(self, source, db, table, primary_key=None, indexes=None, options=None):
        assert self.format is not None, 'Subclass %s must have a format' % self.__class__.__name__
        assert db is not 'rethinkdb', "Error: Cannot import tables into the system database: 'rethinkdb'"
        
        # reporting information
        self._bytes_size   = multiprocessing.Value(ctypes.c_longlong, -1)
        self._bytes_read   = multiprocessing.Value(ctypes.c_longlong, -1)
        
        self._total_rows   = multiprocessing.Value(ctypes.c_longlong, -1)
        self._rows_read    = multiprocessing.Value(ctypes.c_longlong, 0)
        self._rows_written = multiprocessing.Value(ctypes.c_longlong, 0)
        
        # source
        sourceLength = 0
        if hasattr(source, 'read'):
            if unicode != str or 'b' in source.mode:
                # Python2.x or binary file, assume utf-8 encoding
                self._source = codecs.getreader("utf-8")(source)
            else:
                # assume that it has the right encoding on it
                self._source = source
        else:
            try:
                self._source = codecs.open(source, mode="r", encoding="utf-8")
            except IOError as e:
                raise ValueError('Unable to open source file "%s": %s' % (str(source), str(e)))
        
        if hasattr(self._source, 'name') and self._source.name and os.path.isfile(self._source.name):
            self._bytes_size.value = os.path.getsize(source)
            if self._bytes_size.value == 0:
                raise ValueError('Source is zero-length: %s' % source)
        
        # table info
        self.db          = db
        self.table       = table
        self.primary_key = primary_key
        self.indexes     = indexes or []
        
        # options
        self.options     = options
        
        # name
        if hasattr(self._source, 'name') and self._source.name:
            self.name = os.path.basename(self._source.name)
        else:
            self.name = '%s.%s' % (self.db, self.table)
        
    def get_line(self):
        '''Returns a single line from the file'''
        raise NotImplementedError('This needs to be implemented on the %s subclass' % self.format)
    
    # - bytes
    @property
    def bytes_size(self):
        return self._bytes_size.value
    @bytes_size.setter
    def bytes_size(self, value):
        self._bytes_size.value = value
    
    @property
    def bytes_read(self):
        return self._bytes_read.value
    @bytes_read.setter
    def bytes_read(self, value):
        self._bytes_read.value = value
    
    # - rows
    @property
    def total_rows(self):
        return self._total_rows.value
    @total_rows.setter
    def total_rows(self, value):
        self._total_rows.value = value
    
    @property
    def rows_read(self):
        return self._rows_read.value
    @rows_read.setter
    def rows_read(self, value):
        self._rows_read.value = value
    
    @property
    def rows_written(self):
        return self._rows_written.value
    def add_rows_written(self, increment): # we have multiple writers to coordinate
        with self._rows_written.get_lock():
            self._rows_written.value += increment
    
    # - percent done
    @property
    def percentDone(self):
        '''return a float between 0 and 1 for a reasonable guess of percentage complete'''
        # assume that reading takes 50% of the time and writing the other 50%
        completed = 0.0 # of 2.0
        
        # - add read percentage
        if self._bytes_size.value <= 0 or self._bytes_size.value <= self._bytes_read.value:
            completed += 1.0
        elif self._bytes_read.value < 0 and self._total_rows.value >= 0:
            # done by rows read
            if self._rows_read > 0:
                completed += float(self._rows_read) / float(self._total_rows.value)
        else:
            # done by bytes read
            if self._bytes_read.value > 0:
                completed += float(self._bytes_read.value) / float(self._bytes_size.value)
        read = completed
        
        # - add written percentage
        if self._rows_read.value or self._rows_written.value:
            totalRows = float(self._total_rows.value)
            if totalRows == 0:
                completed += 1.0
            elif totalRows < 0:
                # a guesstimate
                perRowSize = float(self._bytes_read.value) / float(self._rows_read.value)
                totalRows = float(self._rows_read.value) + (float(self._bytes_size.value - self._bytes_read.value) / perRowSize)
                completed += float(self._rows_written.value) / totalRows
            else:
                # accurate count
                completed += float(self._rows_written.value) / totalRows
        
        # - return the value
        return completed * 0.5
    
    def setup_table(self, options):
        '''Ensure that the db, table, and indexes exist and are correct'''
        
        # - ensure the table exists and is ready
        utils_common.retryQuery(
            "create table: %s.%s" % (self.db, self.table),
            r.expr([self.table]).set_difference(r.db(self.db).table_list()).for_each(r.db(self.db).table_create(r.row, **options.create_args))
        )
        utils_common.retryQuery("wait for %s.%s" % (self.db, self.table), r.db(self.db).table(self.table).wait(timeout=30))
        
        # - ensure that the primary key on the table is correct
        primaryKey = utils_common.retryQuery(
            "primary key %s.%s" % (self.db, self.table),
            r.db(self.db).table(self.table).info()["primary_key"],
        )
        if self.primary_key is None:
            self.primary_key = primaryKey
        elif primaryKey != self.primary_key:
            raise RuntimeError("Error: table %s.%s primary key was `%s` rather than the expected: %s" % (self.db, table.table, primaryKey, self.primary_key))
        
        # - recreate secondary indexes - dropping existing on the assumption they are wrong
        if options.sindexes:
            existing_indexes = utils_common.retryQuery("indexes from: %s.%s" % (self.db, self.table), r.db(self.db).table(self.table).index_list())
            try:
                created_indexes = []
                for index in self.indexes:
                    if index["index"] in existing_indexes: # drop existing versions
                        utils_common.retryQuery(
                            "drop index: %s.%s:%s" % (self.db, self.table, index["index"]),
                            r.db(self.db).table(self.table).index_drop(index["index"])
                        )
                    utils_common.retryQuery(
                        "create index: %s.%s:%s" % (self.db, self.table, index["index"]),
                        r.db(self.db).table(self.table).index_create(index["index"], index["function"])
                    )
                    created_indexes.append(index["index"])
                
                # wait for all of the created indexes to build
                utils_common.retryQuery(
                    "waiting for indexes on %s.%s" % (self.db, self.table),
                    r.db(self.db).table(self.table).index_wait(r.args(created_indexes))
                )
            except RuntimeError as e:
                ex_type, ex_class, tb = sys.exc_info()
                warning_queue.put((ex_type, ex_class, traceback.extract_tb(tb), self._source.name))
    
    def batches(self, batch_size=None, warning_queue=None):
        
        # default batch_size
        if batch_size is None:
            batch_size = default_batch_size
        else:
            batch_size = int(batch_size)
        assert batch_size > 0
        
        # setup
        self.setup(warning_queue=warning_queue)
        
        # - yield batches
        
        batch = []
        try:
            needMoreData = False
            while True:
                if needMoreData:
                    self.fill_buffer()
                    needMoreData = False
                
                while len(batch) < batch_size:
                    try:
                        row = self.get_line()
                        # ToDo: validate the line
                        batch.append(row)
                    except NeedMoreData:
                        needMoreData = True
                        break
                    except Exception:
                        raise
                else:
                    yield batch
                    batch = []
        
        except StopIteration as e:
            # yield any final batch
            if batch:
                yield batch
        
            # - check the end of the file
        
            self.teardown()
            
            # - 
            raise e
    
    def setup(self, warning_queue=None):
        raise NotImplementedError("Subclasses need to implement this")
    
    def teardown(self):
        pass
    
    def read_to_queue(self, work_queue, exit_event, error_queue, warning_queue, timing_queue, fields=None, ignore_signals=True, batch_size=None):
        if ignore_signals: # ToDo: work out when we are in a worker process automatically
            signal.signal(signal.SIGINT, signal.SIG_IGN) # workers should ignore these
                
        if batch_size is None:
            batch_size = default_batch_size
        
        self.start_time = time.time()
        try:
            timePoint = time.time()
            for batch in self.batches(warning_queue=warning_queue):
                timing_queue.put(('reader_work', time.time() - timePoint))
                timePoint = time.time()
                
                # apply the fields filter
                if fields:
                    for row in batch:
                        for key in [x for x in row.keys() if x not in fields]:
                            del row[key]
                
                while not exit_event.is_set():
                    try:
                        work_queue.put((self.db, self.table, batch), timeout=0.1)
                        self._rows_read.value += len(batch)
                        break
                    except Full:
                        pass
                timing_queue.put(('reader_wait', time.time() - timePoint))
                timePoint = time.time()
        
        # - report relevant errors
        except Exception as e:
            error_queue.put(Error(str(e), traceback.format_exc(), self.name))
            exit_event.set()
            raise
        finally:
            self.end_time = time.time()

class NeedMoreData(Exception):
    pass

class JsonSourceFile(SourceFile):
    format       = 'json'
    
    decoder      = json.JSONDecoder()
    json_array   = None
    found_first  = False
    
    _buffer_size = json_read_chunk_size
    _buffer_str  = None
    _buffer_pos  = None
    _buffer_end  = None
    
    def fill_buffer(self):
        if self._buffer_str is None:
            self._buffer_str = ''
            self._buffer_pos = 0
            self._buffer_end = 0
        elif self._buffer_pos == 0:
            # double the buffer under the assumption that the documents are too large to fit
            if self._buffer_size == json_max_buffer_size:
                raise Exception("Error: JSON max buffer size exceeded on file %s (from position %d). Use '--max-document-size' to extend your buffer." % (self.name, self.bytes_processed))
            self._buffer_size = min(self._buffer_size * 2, json_max_buffer_size)
        
        # add more data
        readTarget = self._buffer_size - self._buffer_end + self._buffer_pos
        assert readTarget > 0
        
        newChunk = self._source.read(readTarget)
        if len(newChunk) == 0:
            raise StopIteration() # file ended
        self._buffer_str = self._buffer_str[self._buffer_pos:] + newChunk
        self._bytes_read.value += len(newChunk)
        
        # reset markers
        self._buffer_pos = 0
        self._buffer_end = len(self._buffer_str) - 1
    
    def get_line(self):
        '''Return a line from the current _buffer_str, or raise NeedMoreData trying'''
        
        # advance over any whitespace
        self._buffer_pos = json.decoder.WHITESPACE.match(self._buffer_str, self._buffer_pos).end()
        if self._buffer_pos >= self._buffer_end:
            raise NeedMoreData()
        
        # read over a comma if we are not the first item in a json_array
        if self.json_array and self.found_first and self._buffer_str[self._buffer_pos] == ",":
            self._buffer_pos += 1
            if self._buffer_pos >= self._buffer_end:
                raise NeedMoreData()
        
        # advance over any post-comma whitespace
        self._buffer_pos = json.decoder.WHITESPACE.match(self._buffer_str, self._buffer_pos).end()
        if self._buffer_pos >= self._buffer_end:
            raise NeedMoreData()
        
        # parse and return an object
        try:
            row, self._buffer_pos = self.decoder.raw_decode(self._buffer_str, idx=self._buffer_pos)
            self.found_first = True
            return row
        except (ValueError, IndexError) as e:
            raise NeedMoreData()
    
    def setup(self, warning_queue=None):
        # - move to the first record
        
        # advance through any leading whitespace
        while True:
            self.fill_buffer()
            self._buffer_pos = json.decoder.WHITESPACE.match(self._buffer_str, 0).end()
            if self._buffer_pos == 0:
                break
        
        # check the first character
        try:
            if self._buffer_str[0] == "[":
                self.json_array = True
                self._buffer_pos = 1
            elif self._buffer_str[0] == "{":
                self.json_array = False
            else:
                raise ValueError("Error: JSON format not recognized - file does not begin with an object or array")
        except IndexError:
            raise ValueError("Error: JSON file was empty of content")
    
        def teardown(self):
            
            # - check the end of the file
            # note: fill_buffer should have guaranteed that we have only the data in the end
            
            # advance through any leading whitespace
            self._buffer_pos = json.decoder.WHITESPACE.match(self._buffer_str, self._buffer_pos).end()
            
            # check the end of the array if we have it
            if self.json_array:
                if self._buffer_str[self._buffer_pos] != "]":
                    snippit = self._buffer_str[self._buffer_pos:]
                    extra = '' if len(snippit) <= 100 else ' and %d more characters' %  (len(snippit) - 100)
                    raise ValueError("Error: JSON array did not end cleanly, rather with: <<%s>>%s" % (snippit[:100], extra))
                self._buffer_pos += 1
            
            # advance through any trailing whitespace
            self._buffer_pos = json.decoder.WHITESPACE.match(self._buffer_str, self._buffer_pos).end()
            snippit = self._buffer_str[self._buffer_pos:]
            if len(snippit) > 0:
                extra = '' if len(snippit) <= 100 else ' and %d more characters' %  (len(snippit) - 100)
                raise ValueError("Error: extra data after JSON data: <<%s>>%s" % (snippit[:100], extra))

class CsvSourceFile(SourceFile):
    format        = "csv"
    
    no_header_row = False
    custom_header = None
    
    _reader       = None # instance of csv.reader
    _columns      = None # name of the columns
    
    def __init__(self, *args, **kwargs):
        if 'options' in kwargs and isinstance(kwargs['options'], dict):
            if 'no_header_row' in kwargs['options']:
                self.no_header_row = kwargs['options']['no_header_row'] == True
                del kwargs['options']['no_header_row']
            if 'custom_header' in kwargs['options']:
                self.custom_header = kwargs['options']['custom_header']
                del kwargs['options']['custom_header']
            if not kwargs['options']:
                del kwargs['options']
            
        super(CsvSourceFile, self).__init__(*args, **kwargs)
    
    def byte_counter(self):
        '''Generator for getting a byte count on a file being used'''
        
        for line in self._source:
            self._bytes_read.value += len(line)
            if unicode != str:
                yield line.encode("utf-8") # Python2.x csv module does not really handle unicode
            else:
                yield line
    
    def setup(self, warning_queue=None):
        # - setup csv.reader with a byte counter wrapper
        
        self._reader = csv.reader(self.byte_counter())
        
        # - get the header information for column names
        
        if not self.no_header_row:
            self._columns = next(self._reader)
        
        # field names may override fields from the header
        if self.custom_header is not None:
            if not self.no_header_row:
                warning_queue.put("Ignoring header row on %s: %s" % (self.name, str(self._columns)))
            self._columns = self.custom_header
        elif self.no_header_row:
            raise ValueError("Error: No field name information available")
    
    def get_line(self):
        rowRaw = next(self._reader)
        if len(self._columns) != len(rowRaw):
            raise Exception("Error: '%s' line %d has an inconsistent number of columns: %s" % (self.name, self._reader.line_num, str(row)))
        
        row = {}
        for key, value in zip(self._columns, rowRaw): # note: we import all csv fields as strings
            # treat empty fields as no entry rather than empty string
            if value == '':
                continue
            row[key] = value if str == unicode else unicode(value, encoding="utf-8")
        
        return row

# ==

usage = """rethinkdb import -d DIR [-c HOST:PORT] [--tls-cert FILENAME] [-p] [--password-file FILENAME]
      [--force] [-i (DB | DB.TABLE)] [--clients NUM]
      [--shards NUM_SHARDS] [--replicas NUM_REPLICAS]
  rethinkdb import -f FILE --table DB.TABLE [-c HOST:PORT] [--tls-cert FILENAME] [-p] [--password-file FILENAME]
      [--force] [--clients NUM] [--format (csv | json)] [--pkey PRIMARY_KEY]
      [--shards NUM_SHARDS] [--replicas NUM_REPLICAS]
      [--delimiter CHARACTER] [--custom-header FIELD,FIELD... [--no-header]]"""
help_epilog = '''
EXAMPLES:

rethinkdb import -d rdb_export -c mnemosyne:39500 --clients 128
  Import data into a cluster running on host 'mnemosyne' with a client port at 39500,
  using 128 client connections and the named export directory.

rethinkdb import -f site_history.csv --format csv --table test.history --pkey count
  Import data into a local cluster and the table 'history' in the 'test' database,
  using the named CSV file, and using the 'count' field as the primary key.

rethinkdb import -d rdb_export -c hades -p -i test
  Import data into a cluster running on host 'hades' which requires a password,
  using only the database 'test' from the named export directory.

rethinkdb import -f subscriber_info.json --fields id,name,hashtag --force
  Import data into a local cluster using the named JSON file, and only the fields
  'id', 'name', and 'hashtag', overwriting any existing rows with the same primary key.

rethinkdb import -f user_data.csv --delimiter ';' --no-header --custom-header id,name,number
  Import data into a local cluster using the named CSV file with no header and instead
  use the fields 'id', 'name', and 'number', the delimiter is a semicolon (rather than
  a comma).
'''

def parse_options(argv, prog=None):
    parser = utils_common.CommonOptionsParser(usage=usage, epilog=help_epilog, prog=prog)
    
    parser.add_option("--clients",         dest="clients",    metavar="CLIENTS",    default=8,      help="client connections to use (default: 8)", type="pos_int")
    parser.add_option("--hard-durability", dest="durability", action="store_const", default="soft", help="use hard durability writes (slower, uses less memory)", const="hard")
    parser.add_option("--force",           dest="force",      action="store_true",  default=False,  help="import even if a table already exists, overwriting duplicate primary keys")
    
    parser.add_option("--batch-size",      dest="batch_size", metavar="BATCH",      default=default_batch_size,          help=optparse.SUPPRESS_HELP, type="pos_int")
    
    # Replication settings
    replicationOptionsGroup = optparse.OptionGroup(parser, "Replication Options")
    replicationOptionsGroup.add_option("--shards",   dest="create_args", metavar="SHARDS",   help="shards to setup on created tables (default: 1)",   type="pos_int", action="add_key")
    replicationOptionsGroup.add_option("--replicas", dest="create_args", metavar="REPLICAS", help="replicas to setup on created tables (default: 1)", type="pos_int", action="add_key")
    parser.add_option_group(replicationOptionsGroup)

    # Directory import options
    dirImportGroup = optparse.OptionGroup(parser, "Directory Import Options")
    dirImportGroup.add_option("-d", "--directory",      dest="directory", metavar="DIRECTORY",   default=None, help="directory to import data from")
    dirImportGroup.add_option("-i", "--import",         dest="db_tables", metavar="DB|DB.TABLE", default=[],   help="restore only the given database or table (may be specified multiple times)", action="append", type="db_table")
    dirImportGroup.add_option("--no-secondary-indexes", dest="sindexes",  action="store_false",  default=None, help="do not create secondary indexes")
    parser.add_option_group(dirImportGroup)

    # File import options
    fileImportGroup = optparse.OptionGroup(parser, "File Import Options")
    fileImportGroup.add_option("-f", "--file", dest="file",         metavar="FILE",        default=None, help="file to import data from", type="file")
    fileImportGroup.add_option("--table",      dest="import_table", metavar="DB.TABLE",    default=None, help="table to import the data into")
    fileImportGroup.add_option("--fields",     dest="fields",       metavar="FIELD,...",   default=None, help="limit which fields to use when importing one table")
    fileImportGroup.add_option("--format",     dest="format",       metavar="json|csv",    default=None, help="format of the file (default: json, accepts newline delimited json)", type="choice", choices=["json", "csv"])
    fileImportGroup.add_option("--pkey",       dest="create_args",  metavar="PRIMARY_KEY", default=None, help="field to use as the primary key in the table", action="add_key")
    parser.add_option_group(fileImportGroup)
    
    # CSV import options
    csvImportGroup = optparse.OptionGroup(parser, "CSV Options")
    csvImportGroup.add_option("--delimiter",     dest="delimiter",     metavar="CHARACTER", default=None, help="character separating fields, or '\\t' for tab")
    csvImportGroup.add_option("--no-header",     dest="no_header",     action="store_true", default=None, help="do not read in a header of field names")
    csvImportGroup.add_option("--custom-header", dest="custom_header", metavar="FIELD,...", default=None, help="header to use (overriding file header), must be specified if --no-header")
    parser.add_option_group(csvImportGroup)
    
    # JSON import options
    jsonOptionsGroup = optparse.OptionGroup(parser, "JSON Options")
    jsonOptionsGroup.add_option("--max-document-size", dest="max_document_size", metavar="MAX_SIZE",  default=0, help="maximum allowed size (bytes) for a single JSON document (default: 128MiB)", type="pos_int")
    jsonOptionsGroup.add_option("--max-nesting-depth", dest="max_nesting_depth", metavar="MAX_DEPTH", default=0, help="maximum depth of the JSON documents (default: 100)", type="pos_int")
    parser.add_option_group(jsonOptionsGroup)
    
    options, args = parser.parse_args(argv)

    # Check validity of arguments

    if len(args) != 0:
        raise parser.error("No positional arguments supported. Unrecognized option(s): %s" % args)
    
    # - create_args
    if options.create_args is None:
        options.create_args = {}
    
    # - options based on file/directory import
    
    if options.directory and options.file:
        parser.error("-f/--file and -d/--directory can not be used together")
    
    elif options.directory:
        if not os.path.exists(options.directory):
            parser.error("-d/--directory does not exist: %s" % options.directory)
        if not os.path.isdir(options.directory):
            parser.error("-d/--directory is not a directory: %s" % options.directory)
        options.directory = os.path.realpath(options.directory)
        
        # disallow invalid options
        if options.import_table:
            parser.error("--table option is not valid when importing a directory")
        if options.fields:
            parser.error("--fields option is not valid when importing a directory")
        if options.format:
            parser.error("--format option is not valid when importing a directory")
        if options.create_args:
            parser.error("--pkey option is not valid when importing a directory")
        
        if options.delimiter:
            parser.error("--delimiter option is not valid when importing a directory")
        if options.no_header:
            parser.error("--no-header option is not valid when importing a directory")
        if options.custom_header:
            parser.error("table create options are not valid when importing a directory: %s" % ", ".join([x.lower().replace("_", " ") for x in options.custom_header.keys()]))
        
        if options.sindexes is None:
            options.sindexes = True
        
        # check valid options
        if not os.path.isdir(options.directory):
            parser.error("Directory to import does not exist: %s" % options.directory)
        
        if options.fields and (len(options.db_tables) > 1 or options.db_tables[0].table is None):
            parser.error("--fields option can only be used when importing a single table")
        
    elif options.file:
        if not os.path.exists(options.file):
            parser.error("-f/--file does not exist: %s" % options.file)
        if not os.path.isfile(options.file):
            parser.error("-f/--file is not a file: %s" % options.file)
        options.file = os.path.realpath(options.file)
        
        # format
        if options.format is None:
            options.format = os.path.splitext(options.file)[1].lstrip('.')
        
        # import_table
        if options.import_table:
            res = utils_common._tableNameRegex.match(options.import_table)
            if res and res.group("table"):
                options.import_table = utils_common.DbTable(res.group("db"), res.group("table"))
            else:
                parser.error("Invalid --table option: %s" % options.import_table)
        
        # fields
        options.fields = options.fields.split(",") if options.fields else None
        
        if options.format == "csv":
            # disallow invalid options
            if options.db_tables:
                parser.error("-i/--import can only be used when importing a directory")
            if options.sindexes:
                parser.error("--no-secondary-indexes can only be used when importing a directory")
            
            if options.max_document_size:
                parser.error("--max_document_size only affects importing JSON documents")
            
            # required options
            if not options.import_table:
                paser.error("A value is required for --table when importing from a file")
            
            # delimiter
            if options.delimiter is None: 
                options.delimiter = ","
            elif options.delimiter == "\\t":
                options.delimiter = "\t"
            elif len(options.delimiter) != 1:
                parser.error("Specify exactly one character for the --delimiter option: %s" % options.delimiter)
            
            # no_header
            if options.no_header is None:
                options.no_header = False
            elif options.custom_header is None:
                parser.error("--custom-header is required if --no-header is specified")
            
            # custom_header
            if options.custom_header:
                options.custom_header = options.custom_header.split(",")
                
        elif options.format == "json": # json format
            # disallow invalid options
            if options.db_tables:
                parser.error("-i/--import can only be used when importing a directory")
            if options.sindexes:
                parser.error("--no-secondary-indexes can only be used when importing a directory")
            
            if options.delimiter is not None:
                parser.error("--delimiter option is not valid for json files")
            if options.no_header is not False:
                parser.error("--no-header option is not valid for json files")
            if options.custom_header is not None:
                parser.error("--custom-header option is not valid for json files")
            
            # default options
            options.format = "json"
            
            if options.max_document_size > 0:
                global json_max_buffer_size
                json_max_buffer_size=options.max_document_size
            
            options.file = os.path.abspath(options.file)
        
        else:
            parser.error("Unrecognized file format: %s" % options.format)
        
    else:
        parser.error("Either -f/--file or -d/--directory is required")
    
    # --
    
    # max_nesting_depth
    if options.max_nesting_depth > 0:
        global max_nesting_depth
        max_nesting_depth = options.max_nesting_depth
    
    # --
    
    return options

# This is run for each client requested, and accepts tasks from the reader processes
def table_writer(tables, options, work_queue, error_queue, warning_queue, exit_event, timing_queue):
    signal.signal(signal.SIGINT, signal.SIG_IGN) # workers should ignore these
    db = table = batch = None
    
    try:
        conflict_action = "replace" if options.force else "error"
        timePoint = time.time()
        while not exit_event.is_set():
            # get a batch
            try:
                db, table, batch = work_queue.get(timeout=0.1)
            except Empty:
                continue
            timing_queue.put(('writer_wait', time.time() - timePoint))
            timePoint = time.time()
            
            # shut down when appropriate
            if isinstance(batch, StopIteration):
                return
            
            # find the table we are working on
            table_info = tables[(db, table)]
            tbl = r.db(db).table(table)
            
            # write the batch to the database
            try:
                res = utils_common.retryQuery(
                    "write batch to %s.%s" % (db, table),
                    tbl.insert(r.expr(batch, nesting_depth=max_nesting_depth), durability=options.durability, conflict=conflict_action)
                )
                
                if res["errors"] > 0:
                    raise RuntimeError("Error when importing into table '%s.%s': %s" % (db, table, res["first_error"]))
                modified = res["inserted"] + res["replaced"] + res["unchanged"]
                if modified != len(batch):
                    raise RuntimeError("The inserted/replaced/unchanged number did not match when importing into table '%s.%s': %s" % (db, table, res["first_error"]))
                
                table_info.add_rows_written(modified)
            
            except r.ReqlError:
                # the error might have been caused by a comm or temporary error causing a partial batch write
                
                for row in batch:
                    if not table_info.primary_key in row:
                        raise RuntimeError("Connection error while importing.  Current row does not have the specified primary key (%s), so cannot guarantee absence of duplicates" % table_info.primary_key)
                    res = None
                    if conflict_action == "replace":
                        res = utils_common.retryQuery(
                            "write row to %s.%s" % (db, table),
                            tbl.insert(r.expr(row, nesting_depth=max_nesting_depth), durability=durability, conflict=conflict_action)
                        )
                    else:
                        existingRow = utils_common.retryQuery(
                            "read row from %s.%s" % (db, table),
                            tbl.get(row[table_info.primary_key])
                        )
                        if not existingRow:
                            res = utils_common.retryQuery(
                                "write row to %s.%s" % (db, table),
                                tbl.insert(r.expr(row, nesting_depth=max_nesting_depth), durability=durability, conflict=conflict_action)
                            )
                        elif existingRow != row:
                            raise RuntimeError("Duplicate primary key `%s`:\n%s\n%s" % (table_info.primary_key, str(row), str(existingRow)))
                    
                    if res["errors"] > 0:
                        raise RuntimeError("Error when importing into table '%s.%s': %s" % (db, table, res["first_error"]))
                    if res["inserted"] + res["replaced"] + res["unchanged"] != 1:
                        raise RuntimeError("The inserted/replaced/unchanged number was not 1 when inserting on '%s.%s': %s" % (db, table, res))
                    table_info.add_rows_written(1)
            timing_queue.put(('writer_work', time.time() - timePoint))
            timePoint = time.time()
            
    except Exception as e:
        error_queue.put(Error(str(e), traceback.format_exc(), "%s.%s" % (db , table)))
        exit_event.set()

def abort_import(pools, exit_event, interrupt_event):
    if interrupt_event.is_set():
        # second time
        print("\nSecond terminate signal seen, aborting ungracefully")
        for pool in pools:
            for worker in pool:
                worker.terminate()
                worker.join(.1)
    else:
        print("\nTerminate signal seen, aborting")
        interrupt_event.set()
        exit_event.set()

def update_progress(tables, options, done_event, exit_event, sleep=0.2):
    signal.signal(signal.SIGINT, signal.SIG_IGN) # workers should not get these
    if options.quiet:
        raise Exception('update_progress called when --quiet was set')
    
    # give weights to each of the tables based on file size
    totalSize = sum([x.bytes_size for x in tables])
    for table in tables:
        table.weight = float(table.bytes_size) / totalSize
    
    lastComplete = None
    startTime    = time.time()
    readWrites   = collections.deque(maxlen=5) # (time, read, write)
    readWrites.append((startTime, 0, 0))
    readRate     = None
    writeRate    = None
    while True:
        try:
            if done_event.is_set() or exit_event.is_set():
                break
            complete = read = write = 0
            currentTime = time.time()
            for table in tables:
                complete += table.percentDone * table.weight
                if options.debug:
                    read     += table.rows_read
                    write    += table.rows_written
            readWrites.append((currentTime, read, write))
            if complete != lastComplete:
                timeDelta = readWrites[-1][0] - readWrites[0][0]
                if options.debug and len(readWrites) > 1:
                    readRate  = max((readWrites[-1][1] - readWrites[0][1]) / timeDelta, 0)
                    writeRate = max((readWrites[-1][2] - readWrites[0][2]) / timeDelta, 0)
                utils_common.print_progress(complete, indent=2, read=readRate, write=writeRate)
                lastComplete = complete
            time.sleep(sleep)
        except KeyboardInterrupt: break
        except Exception as e:
            if options.debug:
                print(e)
                traceback.print_exc()

def import_tables(options, sources):
    # Make sure this isn't a pre-`reql_admin` cluster - which could result in data loss
    # if the user has a database named 'rethinkdb'
    utils_common.check_minimum_version("1.6")
    
    start_time = time.time()
    
    tables = dict(((x.db, x.table), x) for x in sources) # (db, table) => table
    
    work_queue      = Queue(options.clients * 3)
    error_queue     = SimpleQueue()
    warning_queue   = SimpleQueue()
    done_event      = multiprocessing.Event()
    exit_event      = multiprocessing.Event()
    interrupt_event = multiprocessing.Event()
    
    timing_queue    = SimpleQueue()
    timingSums      = {}
    
    pools = []
    progressBar = None
    progressBarSleep = 0.2
    
    # - setup KeyboardInterupt handler
    signal.signal(signal.SIGINT, lambda a, b: abort_import(pools, exit_event, interrupt_event))
    
    # - setup dbs and tables
    
    # create missing dbs
    needed_dbs = set([x.db for x in sources])
    if "rethinkdb" in needed_dbs:
        raise RuntimeError("Error: Cannot import tables into the system database: 'rethinkdb'")
    utils_common.retryQuery("ensure dbs: %s" % ", ".join(needed_dbs), r.expr(needed_dbs).set_difference(r.db_list()).for_each(r.db_create(r.row)))
    
    # check for existing tables, or if --force is enabled ones with mis-matched primary keys
    existing_tables = dict([
        ((x["db"], x["name"]), x["primary_key"]) for x in
        utils_common.retryQuery("list tables", r.db("rethinkdb").table("table_config").pluck(["db", "name", "primary_key"]))
    ])
    already_exist = []
    for source in sources:
        if (source.db, source.table) in existing_tables:
            if not options.force:
                already_exist.append("%s.%s" % (db, table))
            elif source.primary_key is None:
                source.primary_key = existing_tables[(source.db, source.table)]
            elif source.primary_key != existing_tables[(source.db, source.table)]:
                raise RuntimeError("Error: Table '%s.%s' already exists with a different primary key: %s (expected: %s)" % (source.db, source.table, existing_tables[(source.db, source.table)], source.primary_key))
    
    if len(already_exist) == 1:
        raise RuntimeError("Error: Table '%s' already exists, run with --force to import into the existing table" % already_exist[0])
    elif len(already_exist) > 1:
        already_exist.sort()
        raise RuntimeError("Error: The following tables already exist, run with --force to import into the existing tables:\n  %s" % "\n  ".join(already_exist))
    
    # - start the import
    
    try:
        # - start the progress bar
        if not options.quiet:
            progressBar = multiprocessing.Process(
                target=update_progress,
                name="progress bar",
                args=(sources, options, done_event, exit_event, progressBarSleep)
            )
            progressBar.start()
            pools.append([progressBar])
        
        # - start the writers
        writers = []
        pools.append(writers)
        for i in range(options.clients):
            writer = multiprocessing.Process(
                target=table_writer, name="table writer %d" % i,
                kwargs={
                    "tables":tables, "options":options,
                    "work_queue":work_queue, "error_queue":error_queue, "warning_queue":warning_queue, "timing_queue":timing_queue,
                    "exit_event":exit_event
                }
            )
            writers.append(writer)
            writer.start()
        
        # - read the tables options.clients at a time
        readers = []
        pools.append(readers)
        filesLeft = len(sources)
        fileIter = iter(sources)
        try:
            while not exit_event.is_set():
                # add a workers to fill up the readers pool
                while len(readers) < options.clients:
                    table = next(fileIter)
                    reader = multiprocessing.Process(
                        target=table.read_to_queue, name="table reader %s.%s" % (table.db, table.table),
                        kwargs={
                            "fields":options.fields, "batch_size":options.batch_size,
                            "work_queue":work_queue, "error_queue":error_queue, "warning_queue":warning_queue, "timing_queue":timing_queue,
                            "exit_event":exit_event
                        }
                    )
                    readers.append(reader)
                    reader.start()
                    filesLeft -= 1
                
                # drain the timing queue
                while not timing_queue.empty():
                    key, value = timing_queue.get()
                    if not key in timingSums:
                        timingSums[key] = value
                    else:
                        timingSums[key] += value
                
                # reap completed tasks
                for reader in readers[:]:
                    if not reader.is_alive():
                        readers.remove(reader)
                    if filesLeft and len(readers) == options.clients:
                        time.sleep(.05)
        except StopIteration:
            pass # ran out of new tables
        
        # - wait for the last batch of readers to complete
        while readers:
            # drain the timing queue
            while not timing_queue.empty():
                key, value = timing_queue.get()
                if not key in timingSums:
                    timingSums[key] = value
                else:
                    timingSums[key] += value
            
            # watch the readers
            for reader in readers[:]:
                if exit_event.is_set():
                    reader.terminate() # kill it abruptly
                reader.join(.1)
                if not reader.is_alive():
                    readers.remove(reader)
        
        # - append enough StopIterations to signal all writers
        for _ in writers:
            while True:
                if exit_event.is_set():
                    break
                try:
                    work_queue.put((None, None, StopIteration()), timeout=0.1)
                    break
                except Full: pass
        
        # - wait for all of the writers
        for writer in writers[:]:
            while not interrupt_event.is_set():
                if not writer.is_alive():
                    writers.remove(writer)
                    break
                writer.join()
            # kill off the remainder
            if writer in writers:
                try:
                    writer.terminate()
                except Exception: pass
                
        # - stop the progress bar
        if progressBar:
            done_event.set()
            progressBar.join(progressBarSleep * 2)
            if not interrupt_event.is_set():
                utils_common.print_progress(1, indent=2)
            if progressBar.is_alive():
                progressBar.terminate()
        
        # - drain the error_queue
        errors = []
        while not error_queue.empty():
            errors.append(error_queue.get())
        
        # - final reporting
        if not options.quiet:
            # if successful, make sure 100% progress is reported
            if len(errors) == 0 and not interrupt_event.is_set():
                utils_common.print_progress(1.0, indent=2)
            
            # advance past the progress bar
            print('')
            
            # report statistics
            plural = lambda num, text: "%d %s%s" % (num, text, "" if num == 1 else "s")
            print("  %s imported to %s in %.2f secs" % (plural(sum(x.rows_written for x in sources), "row"), plural(len(sources), "table"), time.time() - start_time))
            
            # report debug statistics
            if options.debug:
                print('Debug timing:')
                for key, value in sorted(timingSums.items(), key=lambda x: x[0]):
                    print('  %s: %.2f' % (key, value))
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    
    if interrupt_event.is_set():
        raise RuntimeError("Interrupted")
    
    if len(errors) != 0:
        for error in errors:
            print("%s" % error.message, file=sys.stderr)
            if options.debug and error.traceback:
                print("  Traceback:\n%s" % error.traceback, file=sys.stderr)
            if len(error.file) == 4:
                print("  In file: %s" % error.file, file=sys.stderr)
        raise RuntimeError("Errors occurred during import")
    
    if not warning_queue.empty():
        while not warning_queue.empty():
            warning = warning_queue.get()
            print("%s" % warning[1], file=sys.stderr)
            if options.debug:
                print("%s traceback: %s" % (warning[0].__name__, warning[2]), file=sys.stderr)
            if len(warning) == 4:
                print("In file: %s" % warning[3], file=sys.stderr)
        raise RuntimeError("Warnings occurred during import")

def import_directory(options):
    # Scan for all files, make sure no duplicated tables with different formats
    dbs = False
    sources = {} # (db, table) => {file:, format:, db:, table:, info:}
    files_ignored = []
    for root, dirs, files in os.walk(options.directory):
        if not dbs:
            files_ignored.extend([os.path.join(root, f) for f in files])
            # The first iteration through should be the top-level directory, which contains the db folders
            dbs = True
            
            # don't recurse into folders not matching our filter
            db_filter = set([db_table[0] for db_table in options.db_tables or []])
            if db_filter:
                for dirName in dirs[:]: # iterate on a copy
                    if dirName not in db_filter:
                        dirs.remove(dirName)
        else:
            if dirs:
                files_ignored.extend([os.path.join(root, d) for d in dirs])
                del dirs[:]
            
            db = os.path.basename(root)
            for filename in files:
                path = os.path.join(root, filename)
                table, ext = os.path.splitext(filename)
                table = os.path.basename(table)
                
                if ext not in [".json", ".csv", ".info"]:
                    files_ignored.append(os.path.join(root, filename))
                elif ext == ".info":
                    pass # Info files are included based on the data files
                elif not os.path.exists(os.path.join(root, table + ".info")):
                    files_ignored.append(os.path.join(root, filename))
                else:
                    # ensure we don't have a duplicate
                    if (db, table) in sources:
                        raise RuntimeError("Error: Duplicate db.table found in directory tree: %s.%s" % (db, table))
                    
                    # apply db/table filters
                    if options.db_tables:
                        for filter_db, filter_table in options.db_tables:
                            if db == filter_db and filter_table in (None, table):
                                break # either all tables in this db, or specific pair
                        else:
                            files_ignored.append(os.path.join(root, filename))
                            continue # not a chosen db/table
                    
                    # collect the info
                    primary_key = None
                    indexes = []
                    try:
                        with open(os.path.join(root, table + ".info"), "r") as info_file:
                            metadata = json.load(info_file)
                            if "primary_key" in metadata:
                                primary_key = metadata["primary_key"]
                            if "indexes" in metadata:
                                indexes = metadata["indexes"]
                    except OSError:
                        files_ignored.append(os.path.join(root, f))
                    
                    tableType = JsonSourceFile if ext == ".json" else CsvSourceFile
                    sources[(db, table)] = tableType(
                        source=path,
                        db=db, table=table,
                        primary_key=primary_key,
                        indexes=indexes
                    )

    # Warn the user about the files that were ignored
    if len(files_ignored) > 0:
        print("Unexpected files found in the specified directory.  Importing a directory expects", file=sys.stderr)
        print(" a directory from `rethinkdb export`.  If you want to import individual tables", file=sys.stderr)
        print(" import them as single files.  The following files were ignored:", file=sys.stderr)
        for f in files_ignored:
            print("%s" % str(f), file=sys.stderr)
    
    # start the imports
    import_tables(options, sources.values())

def import_file(options):
    db, table     = options.import_table
    sourceType    = JsonSourceFile
    sourceOptions = {}
    if options.format == "csv":
        sourceOptions = {
            'no_header_row': options.no_header,
            'custom_header': options.custom_header
        }
    import_tables(options, [
        sourceType(
            source=options.file,
            db=db, table=table,
            primary_key=options.create_args.get('primary_key', None) if options.create_args else None,
            options=sourceOptions
        )
    ])

def main(argv=None, prog=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        options = parse_options(argv, prog=prog)
    except RuntimeError as ex:
        print("Usage:\n%s\n%s" % (usage, ex), file=sys.stderr)
        return 1
    
    try:
        start_time = time.time()
        if options.directory:
            import_directory(options)
        elif options.file:
            import_file(options)
        else:
            raise RuntimeError("Error: Neither --directory or --file specified")
    except RuntimeError as ex:
        print(ex, file=sys.stderr)
        if str(ex) == "Warnings occurred during import":
            return 2
        return 1
    if not options.quiet:
        print("  Done (%d seconds)" % (time.time() - start_time))
    return 0

if __name__ == "__main__":
    sys.exit(main())
