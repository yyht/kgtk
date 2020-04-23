"""
Read a KGTK edge file in TSV format.

TODO: Add support for alternative envelope formats, such as JSON.
"""

from argparse import ArgumentParser
import attr
from enum import Enum
from multiprocessing import Process, Queue
from pathlib import Path
import sys
import typing

from kgtk.join.closableiter import ClosableIter, ClosableIterTextIOWrapper
from kgtk.join.enumnameaction import EnumNameAction
from kgtk.join.gzipprocess import GunzipProcess
from kgtk.join.kgtkformat import KgtkFormat

class KgtkReaderErrorAction(Enum):
    """
    Should we complain when errors are detected?

    TODO: Why can't this be inside class KgtkReader?
    """
    SILENT = 0 # Silently ignore the line with the error
    STDOUT = 1 # Print an error message on STDOUT
    STDERR = 2 # Print an error message on STDERR
    VALUEERROR = 4 # Raise a ValueError and fail

@attr.s(slots=True, frozen=False)
class KgtkReader(KgtkFormat, ClosableIter[typing.List[str]]):
    ERROR_LIMIT_DEFAULT: int = 1000
    GZIP_QUEUE_SIZE_DEFAULT: int = GunzipProcess.GZIP_QUEUE_SIZE_DEFAULT

    file_path: typing.Optional[Path] = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(Path)))
    source: ClosableIter[str] = attr.ib() # Todo: validate
    column_names: typing.List[str] = attr.ib(validator=attr.validators.deep_iterable(member_validator=attr.validators.instance_of(str),
                                                                                     iterable_validator=attr.validators.instance_of(list)))
    column_name_map: typing.Mapping[str, int] = attr.ib(validator=attr.validators.deep_mapping(key_validator=attr.validators.instance_of(str),
                                                                                               value_validator=attr.validators.instance_of(int)))

    # For convenience, the count of columns. This is the same as len(column_names).
    column_count: int = attr.ib(validator=attr.validators.instance_of(int))

    data_lines_read: int = attr.ib(validator=attr.validators.instance_of(int), default=0)
    data_lines_passed: int = attr.ib(validator=attr.validators.instance_of(int), default=0)
    data_lines_ignored: int = attr.ib(validator=attr.validators.instance_of(int), default=0)
    data_errors_reported: int = attr.ib(validator=attr.validators.instance_of(int), default=0)

    # The column separator is normally tab.
    column_separator: str = attr.ib(validator=attr.validators.instance_of(str), default=KgtkFormat.COLUMN_SEPARATOR)

    # supply a missing header record or override an existing header record.
    force_column_names: typing.Optional[typing.List[str]] = attr.ib(validator=attr.validators.optional(attr.validators.deep_iterable(member_validator=attr.validators.instance_of(str),
                                                                                                                                     iterable_validator=attr.validators.instance_of(list))),
                                                                    default=None)
    skip_first_record: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False)

    # The index of the mandatory columns.  -1 means missing:
    node1_column_idx: int = attr.ib(validator=attr.validators.instance_of(int), default=-1) # edge file
    node2_column_idx: int = attr.ib(validator=attr.validators.instance_of(int), default=-1) # edge file
    label_column_idx: int = attr.ib(validator=attr.validators.instance_of(int), default=-1) # edge file
    id_column_idx: int = attr.ib(validator=attr.validators.instance_of(int), default=-1) # node file

    # How do we handle errors?
    error_action: KgtkReaderErrorAction = attr.ib(validator=attr.validators.in_(KgtkReaderErrorAction), default=KgtkReaderErrorAction.STDOUT)
    error_limit: int = attr.ib(validator=attr.validators.instance_of(int), default=ERROR_LIMIT_DEFAULT) # >0 ==> limit error reports

    # Ignore empty lines, comments, and all whitespace lines, etc.?
    ignore_empty_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=True)
    ignore_comment_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=True)
    ignore_whitespace_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=True)

    # Ignore records with values in certain fields:
    ignore_blank_node1_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False) # edge file
    ignore_blank_node2_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False) # edge file
    ignore_blank_id_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False) # node file
    
    # Require or fill trailing fields?
    ignore_short_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=True)
    ignore_long_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=True)
    fill_short_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False)
    truncate_long_lines: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False)

    # Other implementation options?
    compression_type: typing.Optional[str] = attr.ib(validator=attr.validators.optional(attr.validators.instance_of(str)), default=None) # TODO: use an Enum
    gzip_in_parallel: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False)
    gzip_queue_size: int = attr.ib(validator=attr.validators.instance_of(int), default=GZIP_QUEUE_SIZE_DEFAULT)

    # Is this an edge file or a node file?
    is_edge_file: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False)
    is_node_file: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False)

    verbose: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False)
    very_verbose: bool = attr.ib(validator=attr.validators.instance_of(bool), default=False)

    class Mode(Enum):
        """
        There are four file reading modes:
        """
        NONE = 0 # Enforce neither edge nore node file required columns
        EDGE = 1 # Enforce edge file required columns
        NODE = 2 # Enforce node file require columns
        AUTO = 3 # Automatically decide whether to enforce edge or node file required columns

    @classmethod
    def open(cls,
             file_path: typing.Optional[Path],
             force_column_names: typing.Optional[typing.List[str]] = None,
             skip_first_record: bool = False,
             fill_short_lines: bool = False,
             truncate_long_lines: bool = False,
             error_action: KgtkReaderErrorAction = KgtkReaderErrorAction.STDOUT,
             error_limit: int = ERROR_LIMIT_DEFAULT,
             ignore_empty_lines: bool = True,
             ignore_comment_lines: bool = True,
             ignore_whitespace_lines: bool = True,
             ignore_blank_node1_lines: bool = True,
             ignore_blank_node2_lines: bool = True,
             ignore_blank_id_lines: bool = True,
             ignore_short_lines: bool = True,
             ignore_long_lines: bool = True,
             compression_type: typing.Optional[str] = None,
             gzip_in_parallel: bool = False,
             gzip_queue_size: int = GZIP_QUEUE_SIZE_DEFAULT,
             column_separator: str = KgtkFormat.COLUMN_SEPARATOR,
             mode: Mode = Mode.AUTO,
             verbose: bool = False,
             very_verbose: bool = False)->"KgtkReader":
        """
        Opens a KGTK file, which may be an edge file or a node file.  The appropriate reader is returned.
        """
        source: ClosableIter[str] = cls._openfile(file_path,
                                                  compression_type=compression_type,
                                                  gzip_in_parallel=gzip_in_parallel,
                                                  gzip_queue_size=gzip_queue_size,
                                                  verbose=verbose)

        # Read the kgtk file header and split it into column names.
        column_names: typing.List[str] = cls._build_column_names(source,
                                                                 force_column_names=force_column_names,
                                                                 skip_first_record=skip_first_record,
                                                                 column_separator=column_separator,
                                                                 verbose=verbose)
        # Build a map from column name to column index.
        column_name_map: typing.Mapping[str, int] = cls.build_column_name_map(column_names)

        # Should we automatically determine if this is an edge file or a node file?
        is_edge_file: bool = False
        is_node_file: bool = False
        if mode is KgtkReader.Mode.AUTO:
            # If we have a node1 (or alias) column, then this must be an edge file. Otherwise, assume it is a node file.
            node1_idx: int = cls.get_column_idx(cls.NODE1_COLUMN_NAMES, column_name_map, is_optional=True)
            is_edge_file = node1_idx >= 0
            is_node_file = not is_edge_file
        elif mode is KgtkReader.Mode.EDGE:
            is_edge_file = True
        elif mode is KgtkReader.Mode.NODE:
            is_node_file = True
        elif mode is KgtkReader.Mode.NONE:
            pass

        if is_edge_file:
            # We'll instantiate an EdgeReader, which is a subclass of KgtkReader.
            # The EdgeReader import is deferred to avoid circular imports.
            from kgtk.join.edgereader import EdgeReader
            
            # Get the indices of the required columns.
            node1_column_idx: int
            node2_column_idx: int
            label_column_idx: int
            (node1_column_idx, node2_column_idx, label_column_idx) = cls.required_edge_columns(column_name_map)

            if verbose:
                print("KgtkReader: Reading an edge file. node1=%d label=%d node2=%d" % (node1_column_idx, label_column_idx, node2_column_idx))

            return EdgeReader(file_path=file_path,
                              source=source,
                              column_separator=column_separator,
                              column_names=column_names,
                              column_name_map=column_name_map,
                              column_count=len(column_names),
                              node1_column_idx=node1_column_idx,
                              node2_column_idx=node2_column_idx,
                              label_column_idx=label_column_idx,
                              force_column_names=force_column_names,
                              skip_first_record=skip_first_record,
                              fill_short_lines=fill_short_lines,
                              truncate_long_lines=truncate_long_lines,
                              error_action=error_action,
                              error_limit=error_limit,
                              ignore_empty_lines=ignore_empty_lines,
                              ignore_comment_lines=ignore_comment_lines,
                              ignore_whitespace_lines=ignore_whitespace_lines,
                              ignore_blank_node1_lines=ignore_blank_node1_lines,
                              ignore_blank_node2_lines=ignore_blank_node2_lines,
                              ignore_short_lines=ignore_short_lines,
                              ignore_long_lines=ignore_long_lines,
                              compression_type=compression_type,
                              gzip_in_parallel=gzip_in_parallel,
                              gzip_queue_size=gzip_queue_size,
                              is_edge_file=is_edge_file,
                              is_node_file=is_node_file,
                              verbose=verbose,
                              very_verbose=very_verbose,
            )
        elif is_node_file:
            # We'll instantiate an NodeReader, which is a subclass of KgtkReader.
            # The NodeReader import is deferred to avoid circular imports.
            from kgtk.join.nodereader import NodeReader
            
            # Get the index of the required column:
            id_column_idx: int = cls.required_node_column(column_name_map)

            if verbose:
                print("KgtkReader: Reading an node file. id=%d" % (id_column_idx))

            return NodeReader(file_path=file_path,
                              source=source,
                              column_separator=column_separator,
                              column_names=column_names,
                              column_name_map=column_name_map,
                              column_count=len(column_names),
                              id_column_idx=id_column_idx,
                              force_column_names=force_column_names,
                              skip_first_record=skip_first_record,
                              fill_short_lines=fill_short_lines,
                              truncate_long_lines=truncate_long_lines,
                              error_action=error_action,
                              error_limit=error_limit,
                              ignore_empty_lines=ignore_empty_lines,
                              ignore_comment_lines=ignore_comment_lines,
                              ignore_whitespace_lines=ignore_whitespace_lines,
                              ignore_blank_id_lines=ignore_blank_id_lines,
                              ignore_short_lines=ignore_short_lines,
                              ignore_long_lines=ignore_long_lines,
                              compression_type=compression_type,
                              gzip_in_parallel=gzip_in_parallel,
                              gzip_queue_size=gzip_queue_size,
                              is_edge_file=is_edge_file,
                              is_node_file=is_node_file,
                              verbose=verbose,
                              very_verbose=very_verbose,
            )
        else:
            return cls(file_path=file_path,
                       source=source,
                       column_separator=column_separator,
                       column_names=column_names,
                       column_name_map=column_name_map,
                       column_count=len(column_names),
                       force_column_names=force_column_names,
                       skip_first_record=skip_first_record,
                       fill_short_lines=fill_short_lines,
                       truncate_long_lines=truncate_long_lines,
                       error_action=error_action,
                       error_limit=error_limit,
                       ignore_empty_lines=ignore_empty_lines,
                       ignore_comment_lines=ignore_comment_lines,
                       ignore_whitespace_lines=ignore_whitespace_lines,
                       ignore_blank_id_lines=ignore_blank_id_lines,
                       ignore_short_lines=ignore_short_lines,
                       ignore_long_lines=ignore_long_lines,
                       compression_type=compression_type,
                       gzip_in_parallel=gzip_in_parallel,
                       gzip_queue_size=gzip_queue_size,
                       is_edge_file=is_edge_file,
                       is_node_file=is_node_file,
                       verbose=verbose,
                       very_verbose=very_verbose,
            )

    @classmethod
    def _open_compressed_file(cls,
                              compression_type: str,
                              file_name: str,
                              file_or_path: typing.Union[Path, typing.TextIO],
                              who: str,
                              verbose: bool)->typing.TextIO:
        
        # TODO: find a better way to coerce typing.IO[Any] to typing.TextIO
        if compression_type in [".gz", "gz"]:
            if verbose:
                print("%s: reading gzip %s" % (who, file_name))
            return gzip.open(file_or_path, mode="rt") # type: ignore
        
        elif compression_type in [".bz2", "bz2"]:
            if verbose:
                print("%s: reading bz2 %s" % (who, file_name))
            return bz2.open(file_or_path, mode="rt") # type: ignore
        
        elif compression_type in [".xz", "xz"]:
            if verbose:
                print("%s: reading lzma %s" % (who, file_name))
            return lzma.open(file_or_path, mode="rt") # type: ignore
        
        elif compression_type in [".lz4", "lz4"]:
            if verbose:
                print("%s: reading lz4 %s" % (who, file_name))
            return lz4.frame.open(file_or_path, mode="rt") # type: ignore
        else:
            # TODO: throw a better exception.
                raise ValueError("%s: Unexpected compression_type '%s'" % (who, compression_type))

    @classmethod
    def _openfile(cls, file_path: typing.Optional[Path],
                  compression_type: typing.Optional[str],
                  gzip_in_parallel: bool,
                  gzip_queue_size: int,
                  verbose: bool)->ClosableIter[str]:
        who: str = cls.__name__
        if file_path is None or str(file_path) == "-":
            if compression_type is not None and len(compression_type) > 0:
                return ClosableIterTextIOWrapper(cls._open_compressed_file(compression_type, "-", sys.stdin, who, verbose))
            else:
                if verbose:
                    print("%s: reading stdin" % who)
                return ClosableIterTextIOWrapper(sys.stdin)

        if verbose:
            print("%s: File_path.suffix: %s" % (who, file_path.suffix))

        gzip_file: typing.TextIO
        if compression_type is not None and len(compression_type) > 0:
            gzip_file = cls._open_compressed_file(compression_type, str(file_path), file_path, who, verbose)
        elif file_path.suffix in [".bz2", ".gz", ".lz4", ".xz"]:
            gzip_file = cls._open_compressed_file(file_path.suffix, str(file_path), file_path, who, verbose)
        else:
            if verbose:
                print("%s: reading file %s" % (who, str(file_path)))
            return ClosableIterTextIOWrapper(open(file_path, "r"))

        if gzip_in_parallel:
            gzip_thread: GunzipProcess = GunzipProcess(gzip_file, Queue(gzip_queue_size))
            gzip_thread.start()
            return gzip_thread
        else:
            return ClosableIterTextIOWrapper(gzip_file)
            

    @classmethod
    def _build_column_names(cls,
                            source: ClosableIter[str],
                            force_column_names: typing.Optional[typing.List[str]],
                            skip_first_record: bool,
                            column_separator: str,
                            verbose: bool = False,
    )->typing.List[str]:
        """
        Read the kgtk file header and split it into column names.
        """
        column_names: typing.List[str]
        if force_column_names is None:
            # Read the column names from the first line, stripping end-of-line characters.
            #
            # TODO: if the read fails, throw a more useful exception with the line number.
            header: str = next(source).rstrip("\r\n")
            if verbose:
                print("header: %s" % header)


            # Split the first line into column names.
            column_names = header.split(column_separator)
        else:
            # Skip the first record to override the column names in the file.
            # Do not skip the first record if the file does not hae a header record.
            if skip_first_record:
                next(source)
            # Use the forced column names.
            column_names = force_column_names
        return column_names

    def close(self):
        self.source.close()

    def yelp(self, msg: str, line: str):
        self.data_lines_ignored += 1
        if self.error_action == KgtkReaderErrorAction.SILENT:
            return
        elif self.error_action == KgtkReaderErrorAction.STDOUT:
            print("In input data line %d, %s: %s" % (self.data_lines_read, msg, line))
        elif self.error_action == KgtkReaderErrorAction.STDERR:
            print("In input data line %d, %s: %s" % (self.data_lines_read, msg, line), file=sys.stderr)
        elif self.error_action == KgtkReaderErrorAction.VALUEERROR:
            raise ValueError("In input data line %d, %s: %s" % (self.data_lines_read, msg, line))
        self.data_errors_reported += 1
        if self.error_limit > 0 and self.data_errors_reported >= self.error_limit:
            raise ValueError("Too many data errors.")

    # This is both and iterable and an iterator object.
    def __iter__(self)->typing.Iterator[typing.List[str]]:
        return self

    # Get the next edge values as a list of strings.
    # TODO: Convert integers, coordinates, etc. to Python types
    def __next__(self)-> typing.List[str]:
        values: typing.List[str]

        # This loop accomodates lines that are ignored.
        while (True):
            line: str
            try:
                
                line = next(self.source) # Will throw StopIteration
            except StopIteration as e:
                # Close the input file!
                #
                # TODO: implement a close() routine and/or whatever it takes to support "with".
                self.source.close() # Do we need to guard against repeating this call?
                raise e

            # Count the data line read.
            self.data_lines_read += 1

            # Strip the end-of-line characters:
            line = line.rstrip("\r\n")

            if self.very_verbose:
                print("'%s'" % line)

            # Ignore empty lines.
            if len(line) == 0 and self.ignore_empty_lines:
                self.yelp("saw an empty line", line)
                continue
            # Ignore comment lines:
            if line[0] == self.COMMENT_INDICATOR and self.ignore_comment_lines:
                self.yelp("saw a comment line", line)
                continue
            # Ignore whitespace lines
            if self.ignore_whitespace_lines and line.isspace():
                self.yelp("saw a whitespace line", line)
                continue

            values = line.split(self.column_separator)

            # Optionally fill missing trailing columns with empty values:
            if self.fill_short_lines and len(values) < self.column_count:
                while len(values) < self.column_count:
                    values.append("")
                    
            # Optionally remove extra trailing columns:
            if self.truncate_long_lines and len(values) > self.column_count:
                values = values[:self.column_count]

            # Optionally validate that the line contained the right number of columns:
            #
            # When we report line numbers in error messages, line 1 is the first line after the header line.
            if self.ignore_short_lines and len(values) < self.column_count:
                self.yelp("Required %d columns, saw %d: '%s'" % (self.column_count,
                                                                   len(values),
                                                                   line),
                            line)
                continue
                             
            if self.ignore_long_lines and len(values) > self.column_count:
                self.yelp("Required %d columns, saw %d (%d extra): '%s'" % (self.column_count,
                                                                              len(values),
                                                                              len(values) - self.column_count,
                                                                              line),
                            line)
                continue

            if self._ignore_if_blank_fields(values):
                self.yelp("saw blank values in required fields", line)
                continue

            self.data_lines_passed += 1
            if self.very_verbose:
                sys.stdout.write(".")
                sys.stdout.flush()
            
            return values

    # May be overridden
    def _ignore_if_blank_fields(self, values: typing.List[str]):
        return False

    # May be overridden
    def _skip_reserved_fields(self, column_name):
        return False

    def additional_column_names(self)->typing.List[str]:
        if self.is_edge_file:
            return KgtkFormat.additional_edge_columns(self.column_names)
        elif self.is_node_file:
            return KgtkFormat.additional_node_columns(self.column_names)
        else:
            # TODO: throw a better exception.
            raise ValueError("KgtkReader: Unknown Kgtk file type.")

    def merge_columns(self, additional_columns: typing.List[str])->typing.List[str]:
        """
        Return a list that merges the current column names with an additional set
        of column names.

        """
        merged_columns: typing.List[str] = self.column_names.copy()

        column_name: str
        for column_name in additional_columns:
            if column_name in self.column_name_map:
                continue
            merged_columns.append(column_name)

        return merged_columns

    def to_map(self, line: typing.List[str])->typing.Mapping[str, str]:
        """
        Convert an input line into a named map of fields.
        """
        result: typing.MutableMapping[str, str] = { }
        value: str
        idx: int = 0
        for value in line:
            result[self.column_names[idx]] = value
            idx += 1
        return result

    @classmethod
    def add_shared_arguments(cls, parser: ArgumentParser):
        parser.add_argument(dest="kgtk_file", help="The KGTK file to read", type=Path, default=None)

        parser.add_argument(      "--allow-comment-lines", dest="ignore_comment_lines",
                                  help="When specified, do not ignore comment lines.", action='store_false')

        parser.add_argument(      "--allow-empty-lines", dest="ignore_empty_lines",
                                  help="When specified, do not ignore empty lines.", action='store_false')

        parser.add_argument(      "--allow-long-lines", dest="ignore_long_lines",
                                  help="When specified, do not ignore lines with extra columns.", action='store_false')

        parser.add_argument(      "--allow-short-lines", dest="ignore_short_lines",
                                  help="When specified, do not ignore lines with missing columns.", action='store_false')

        parser.add_argument(      "--allow-whitespace-lines", dest="ignore_whitespace_lines",
                                  help="When specified, do not ignore whitespace lines.", action='store_false')

        parser.add_argument(      "--column-separator", dest="column_separator",
                                  help="Column separator.", type=str, default=cls.COLUMN_SEPARATOR)

        parser.add_argument(      "--compression-type", dest="compression_type", help="Specify the compression type.")

        parser.add_argument(      "--error-action", dest="error_action",
                                  help="The action to take for error input lines",
                                  type=KgtkReaderErrorAction, action=EnumNameAction)

        parser.add_argument(      "--error-limit", dest="error_limit",
                                  help="The maximum number of errors to report before failing", type=int, default=cls.ERROR_LIMIT_DEFAULT)

        parser.add_argument(      "--fill-short-lines", dest="fill_short_lines",
                                  help="Fill missing trailing columns in short lines with empty values.", action='store_true')

        parser.add_argument(      "--force-column-names", dest="force_column_names", help="Force the column names.", nargs='*')

        parser.add_argument(      "--gzip-in-parallel", dest="gzip_in_parallel", help="Execute gzip in parallel.", action='store_true')

        parser.add_argument(      "--gzip-queue-size", dest="gzip_queue_size",
                                  help="Queue size for parallel gzip.", type=int, default=cls.GZIP_QUEUE_SIZE_DEFAULT)

        parser.add_argument(      "--skip-first-record", dest="skip_first_record", help="Skip the first record when forcing column names.", action='store_true')

        parser.add_argument(      "--truncate-long-lines", dest="truncate_long_lines",
                                  help="Remove excess trailing columns in long lines.", action='store_true')

        parser.add_argument("-v", "--verbose", dest="verbose", help="Print additional progress messages.", action='store_true')

        parser.add_argument(      "--very-verbose", dest="very_verbose", help="Print additional progress messages.", action='store_true')

    # May be overridden
    @classmethod
    def add_arguments(cls, parser: ArgumentParser):
        parser.add_argument(      "--mode", dest="mode",
                                  help="Determine the KGTK file mode.", type=KgtkReader.Mode, action=EnumNameAction, default=KgtkReader.Mode.AUTO)


    
def main():
    """
    Test the KGTK file reader.
    """
    parser = ArgumentParser()
    KgtkReader.add_shared_arguments(parser)
    KgtkReader.add_arguments(parser)
    EdgeReader.add_arguments(parser)
    NodeReader.add_arguments(parser)
    args = parser.parse_args()

    kr: KgtkReader = KgtkReader.open(args.kgtk_file,
                                     force_column_names=args.force_column_names,
                                     skip_first_record=args.skip_first_record,
                                     fill_short_lines=args.fill_short_lines,
                                     truncate_long_lines=args.truncate_long_lines,
                                     error_action=args.error_action,
                                     error_limit=args.error_limit,
                                     ignore_comment_lines=args.ignore_comment_lines,
                                     ignore_whitespace_lines=args.ignore_whitespace_lines,
                                     ignore_blank_node1_lines=args.ignore_blank_node1_lines,
                                     ignore_blank_node2_lines=args.ignore_blank_node2_lines,
                                     ignore_blank_id_lines=args.ignore_blank_id_lines,
                                     ignore_short_lines=args.ignore_short_lines,
                                     ignore_long_lines=args.ignore_long_lines,
                                     compression_type=args.compression_type,
                                     gzip_in_parallel=args.gzip_in_parallel,
                                     gzip_queue_size=args.gzip_queue_size,
                                     column_separator=args.column_separator,
                                     mode=args.mode,
                                     verbose=args.verbose, very_verbose=args.very_verbose)

    line_count: int = 0
    line: typing.List[str]
    for line in kr:
        line_count += 1
    print("Read %d lines" % line_count)

if __name__ == "__main__":
    main()