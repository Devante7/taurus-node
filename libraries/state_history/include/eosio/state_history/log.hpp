#pragma once

#include <boost/filesystem.hpp>
#include <fstream>
#include <stdint.h>

#include <cstddef>
#include <eosio/chain/block_header.hpp>
#include <eosio/chain/db_util.hpp>
#include <eosio/chain/exceptions.hpp>
#include <eosio/chain/log_catalog.hpp>
#include <eosio/chain/log_data_base.hpp>
#include <eosio/chain/log_index.hpp>
#include <eosio/chain/types.hpp>
#include <eosio/state_history/transaction_trace_cache.hpp>
#include <fc/bitutil.hpp>
#include <fc/io/cfile.hpp>
#include <fc/io/datastream.hpp>
#include <fc/log/logger.hpp>
#include <boost/asio.hpp>

namespace eosio {

namespace bfs = boost::filesystem;

/*
 *   *.log:
 *   +---------+----------------+-----------+------------------+-----+---------+----------------+
 *   | Entry i | Pos of Entry i | Entry i+1 | Pos of Entry i+1 | ... | Entry z | Pos of Entry z |
 *   +---------+----------------+-----------+------------------+-----+---------+----------------+
 *
 *   *.index:
 *   +----------------+------------------+-----+----------------+
 *   | Pos of Entry i | Pos of Entry i+1 | ... | Pos of Entry z |
 *   +----------------+------------------+-----+----------------+
 *
 * each entry:
 *    state_history_log_header
 *    payload
 */

inline uint64_t       ship_magic(uint32_t version) {
   using namespace eosio::chain::literals;
   return "ship"_n.to_uint64_t() | version;
}
inline bool           is_ship(uint64_t magic) {
   using namespace eosio::chain::literals;
   return (magic & 0xffff'ffff'0000'0000) == "ship"_n.to_uint64_t();
}
inline uint32_t       get_ship_version(uint64_t magic) { return magic; }
inline bool           is_ship_supported_version(uint64_t magic) { return get_ship_version(magic) <= 1; }
static const uint32_t ship_current_version = 1;

struct state_history_log_header {
   uint64_t             magic        = ship_magic(ship_current_version);
   chain::block_id_type block_id     = {};
   uint64_t             payload_size = 0;
};
static const int state_history_log_header_serial_size = sizeof(state_history_log_header::magic) +
                                                        sizeof(state_history_log_header::block_id) +
                                                        sizeof(state_history_log_header::payload_size);

class state_history_log_data : public chain::log_data_base<state_history_log_data> {
   std::string filename;

 public:
   state_history_log_data() = default;
   state_history_log_data(const fc::path& path, mapmode mode = mapmode::readonly)
       : filename(path.string()) {
      open(path, mode);
   }

   void open(const fc::path& path, mapmode mode = mapmode::readonly) {
      if (file.is_open())
         file.close();
      file.open(path.string(), mode);
      return;
   }

   uint32_t version() const { return get_ship_version(chain::read_buffer<uint64_t>(file.const_data())); }
   uint32_t first_block_num() const { return block_num_at(0); }
   uint32_t first_block_position() const { return 0; }

   std::pair<fc::datastream<const char*>, uint32_t> ro_stream_at(uint64_t pos) const {
      uint32_t ver = get_ship_version(chain::read_buffer<uint64_t>(file.const_data() + pos));
      return std::make_pair(
          fc::datastream<const char*>(file.const_data() + pos + sizeof(state_history_log_header), payload_size_at(pos)),
          ver);
   }

   std::pair<fc::datastream<char*>, uint32_t> rw_stream_at(uint64_t pos) const {
      uint32_t ver = get_ship_version(chain::read_buffer<uint64_t>(file.const_data() + pos));
      return std::make_pair(
          fc::datastream<char*>(file.data() + pos + sizeof(state_history_log_header), payload_size_at(pos)), ver);
   }

   uint32_t block_num_at(uint64_t position) const {
      return fc::endian_reverse_u32(
          chain::read_buffer<uint32_t>(file.const_data() + position + offsetof(state_history_log_header, block_id)));
   }

   chain::block_id_type block_id_at(uint64_t position) const {
      return chain::read_buffer<chain::block_id_type>(file.const_data() + position +
                                                      offsetof(state_history_log_header, block_id));
   }

   uint64_t payload_size_at(uint64_t pos) const;
   void     construct_index(const fc::path& index_file_name) const;
};

struct state_history_config {
   bfs::path log_dir;
   bfs::path retained_dir;
   bfs::path archive_dir;
   uint32_t  stride             = UINT32_MAX;
   uint32_t  max_retained_files = 10;
   fc::logger* logger             = nullptr;
};

class state_history_log {
 protected:
   using cfile_stream        = fc::datastream<fc::cfile>;
 private:
   const char* const    name = "";
   uint32_t             version = ship_current_version;
   uint32_t             stride;   

 public:
   // The type aliases below help to make it obvious about the meanings of member function return values.
   using block_num_type     = uint32_t;
   using version_type       = uint32_t;
   using file_position_type = uint64_t;
   using config_type        = state_history_config;

   

   state_history_log(const char* const name, const state_history_config& conf);
   virtual ~state_history_log() { stop(); }

   std::pair<block_num_type, block_num_type> begin_end_block_nums() { 
      std::lock_guard<std::mutex> lock(mx);
      block_num_type result = catalog.first_block_num();
      return std::make_pair(result != 0 ? result : _begin_block, _end_block);
   }

   std::optional<chain::block_id_type> get_block_id(block_num_type block_num);
   void                                stop();
   void                                light_stop();

 protected:
   void store_entry(const chain::block_id_type& id, const chain::block_id_type& prev_id, std::vector<char>&& data);
   std::optional<state_history_log_header> get_entry_header(block_num_type block_num);
   std::pair<std::vector<char>, uint32_t> read_entry(block_num_type block_num);

   template <typename Lambda>
   void modify_entry(block_num_type block_num, Lambda f);

   using cached_data_map = boost::container::flat_map<uint32_t, std::shared_ptr<std::vector<char>>>;
   cached_data_map cached;
   fc::logger      logger;

 private:

   std::thread                                                              thr;
   uint32_t                                                                 num_buffered_entries = 2;
   std::atomic<bool>                                                        write_thread_has_exception = false;
   std::exception_ptr                                                       eptr;
   boost::asio::io_context                                                  ctx;
   boost::asio::io_context::strand                                          work_strand{ctx};
   boost::asio::executor_work_guard<boost::asio::io_context::executor_type> work_guard =
       boost::asio::make_work_guard(ctx);

   using catalog_t = chain::log_catalog<state_history_log_data, chain::log_index<chain::state_history_exception>>;

   chain::block_id_type last_block_id;

   std::mutex           mx; // the mutex is specifically to protect the access of the following variables
   uint32_t             _begin_block = 0;
   uint32_t             _end_block   = 0;
   catalog_t            catalog;
   cfile_stream         read_log;
   cfile_stream         index;

   // not protected by mx, but always accessed from work_strand
   cfile_stream         write_log;

 private:
   void               read_header(state_history_log_header& header, bool assert_version = true);
   void               write_header(const state_history_log_header& header);
   bool               get_last_block(uint64_t size);
   void               recover_blocks(uint64_t size);
   void               open_log(bfs::path filename);
   void               open_index(bfs::path filename);
   file_position_type get_pos(block_num_type block_num);
   void               truncate(block_num_type block_num);
   void               split_log();

   state_history_log_header get_entry_header_i(block_num_type block_num);

   /**
    *  @returns the block num and the file position
    **/
   std::pair<block_num_type,file_position_type> write_entry_header(const state_history_log_header& header, const chain::block_id_type& prev_id);
   void write_entry_position(const state_history_log_header& header, file_position_type pos, block_num_type block_num);
   void write_entry(const chain::block_id_type& id, const chain::block_id_type& prev_id, const std::shared_ptr<std::vector<char>>& data);

   virtual void write_payload(cfile_stream& stream, const std::vector<char>& data) = 0;
}; // state_history_log

class state_history_traces_log : public state_history_log {
   std::map<uint32_t,state_history::transaction_trace_cache> trace_caches;

 public:
   bool                            trace_debug_mode = false;
   state_history::compression_type compression      = state_history::compression_type::zlib;

   state_history_traces_log(const state_history_config& conf);

   static bool exists(bfs::path state_history_dir);

   void add_transaction(const chain::transaction_trace_ptr& trace, const chain::packed_transaction_ptr& transaction) {
      trace_caches[trace->block_num].add_transaction(trace, transaction);
   }

   std::shared_ptr<std::vector<char>> get_log_entry(block_num_type block_num);

   void block_start(uint32_t block_num) {
      trace_caches[block_num].clear();
   }

   void store(const chainbase::database& db, const chain::block_state_ptr& block_state);

   /**
    *  @param[in,out] ids The ids to been pruned and returns the ids not found in the specified block
    **/
   void prune_transactions(block_num_type block_num, std::vector<chain::transaction_id_type>& ids);
 private:
   void write_payload(cfile_stream& stream, const std::vector<char>& data) override;
};

class state_history_chain_state_log : public state_history_log {
 public:
   state_history_chain_state_log(const state_history_config& conf);

   std::shared_ptr<std::vector<char>> get_log_entry(block_num_type block_num);

   void store(const chainbase::database& db, const chain::block_state_ptr& block_state);
 private:
   void write_payload(cfile_stream& stream, const std::vector<char>& data) override;
};

} // namespace eosio

FC_REFLECT(eosio::state_history_log_header, (magic)(block_id)(payload_size))
